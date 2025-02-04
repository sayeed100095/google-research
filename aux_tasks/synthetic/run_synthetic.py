# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Implicit aux tasks training.

Example command:

python -m aux_tasks.synthetic.run_synthetic

"""
# pylint: disable=invalid-name
import functools
import pickle
from typing import Callable

from absl import app
from absl import flags
from absl import logging
from clu import checkpoint
from clu import metric_writers
from clu import periodic_actions
from etils import epath
from etils import etqdm
import jax
import jax.numpy as jnp
from ml_collections import config_dict
from ml_collections import config_flags
import numpy as np
import optax

from aux_tasks.synthetic import estimates
from aux_tasks.synthetic import utils

_config = config_dict.ConfigDict()

_config.method: str = 'explicit'
_config.optimizer: str = 'sgd'
_config.num_epochs: int = 200_000
_config.rescale_psi = ''
_config.use_mnist = False
_config.sample_with_replacement = True
_config.use_tabular_gradient = True

_config.S: int = 10  # Number of states
_config.T: int = 10  # Number of aux. tasks
_config.d: int = 1  # feature dimension

_config.estimate_feature_norm: bool = True

# The theoretical maximum for kappa is 2, and 1.9 works well.
_config.kappa: float = 1.9  # Lissa kappa

_config.covariance_batch_size: int = 32
_config.main_batch_size: int = 32
_config.weight_batch_size: int = 32

_config.seed: int = 4753849
_config.lr: float = 0.01

# If the SVD has precomputed, supply the path here to avoid recomputing it.
_config.svd_path: str = ''

_WORKDIR = flags.DEFINE_string(
    'workdir', None, 'Base directory to store stats.', required=True)
_CONFIG = config_flags.DEFINE_config_dict('config', _config, lock_config=True)


def compute_optimal_subspace(Psi, d):
  left_svd, _, _ = jnp.linalg.svd(Psi)
  return left_svd[:, :d]


def compute_grassman_distance(Y1, Y2):
  """Grassman distance between subspaces spanned by Y1 and Y2."""
  Q1, _ = jnp.linalg.qr(Y1)
  Q2, _ = jnp.linalg.qr(Y2)

  _, sigma, _ = jnp.linalg.svd(Q1.T @ Q2)
  sigma = jnp.round(sigma, decimals=6)
  return jnp.linalg.norm(jnp.arccos(sigma))


def compute_cosine_similarity(Y1, Y2):
  try:
    projection_weights = jnp.linalg.solve(Y1.T @ Y1, Y1.T @ Y2)
    projection = Y1 @ projection_weights

    return jnp.linalg.norm(projection)
  except np.linalg.LinAlgError:
    pass
  return jnp.nan


def compute_normalized_dot_product(Y1,
                                   Y2):
  return jnp.abs(
      jnp.squeeze(Y1.T @ Y2 / (jnp.linalg.norm(Y1) * jnp.linalg.norm(Y2))))


def eigengame_subspace_distance(Phi,
                                optimal_subspace):
  """Compute subspace distance as per the eigengame paper."""
  try:
    d = Phi.shape[1]
    U_star = optimal_subspace @ optimal_subspace.T

    U_phi, _, _ = jnp.linalg.svd(Phi)
    U_phi = U_phi[:, :d]
    P_star = U_phi @ U_phi.T

    return 1 - 1 / d * jnp.trace(U_star @ P_star)
  except np.linalg.LinAlgError:
    return jnp.nan


def compute_metrics(Phi,
                    optimal_subspace):
  """Computes a variety of learning curve-type metrics for the given run.

  Args:
    Phi: Feature matrix.
    optimal_subspace: The optimal subspace.

  Returns:
    dict with keys:
      cosine_similarity: a jnp.array of size num_update_steps with cosine
        similarity between Phi and the d-principal subspace of Psi.
      feature_norm: the mean norm of the state feature vectors
        (averaged across states) over time.
  """
  feature_norm = jnp.linalg.norm(Phi) / Phi.shape[0]
  cosine_similarity = compute_cosine_similarity(Phi, optimal_subspace)

  metrics = {
      'cosine_similarity':
          cosine_similarity,
      'feature_norm':
          feature_norm,
      'eigengame_subspace_distance':
          eigengame_subspace_distance(Phi, optimal_subspace)
  }

  _, d = Phi.shape
  if d > 1:
    grassman_distance = compute_grassman_distance(Phi, optimal_subspace)
    metrics |= {'grassman_distance': grassman_distance}
  elif d == 1:
    dot_product = compute_normalized_dot_product(Phi, optimal_subspace)
    metrics |= {'dot_product': dot_product}

  return metrics


@functools.partial(
    jax.jit,
    static_argnames=(
        'compute_psi',
        'optimizer',
        'method',
        'covariance_batch_size',
        'main_batch_size',
        'covariance_batch_size',
        'weight_batch_size',
        'num_tasks',
        'estimate_feature_norm',
        'sample_states',
        'use_tabular_gradient',
    ))
def _train_step(
    *,
    Phi,
    compute_psi,
    optimizer,
    optimizer_state,
    explicit_weight_matrix,
    estimated_feature_norm,
    learning_rate,
    key,
    method,
    lissa_kappa,
    main_batch_size,
    covariance_batch_size,
    weight_batch_size,
    num_tasks,
    estimate_feature_norm = True,
    sample_states,
    use_tabular_gradient = True):
  """Computes one training step.

  Args:
    Phi: The current feature matrix.
    compute_psi: A function implementing a mapping from (state, task) pairs
      to real values. In the finite case, this can be implemented
      as a function that indexes into a matrix. Note: the code does
      not currently support an infinite number of tasks.
    optimizer: An optax optimizer to use.
    optimizer_state: The current state of the optimizer.
    explicit_weight_matrix: A weight matrix to use for the explicit method.
    estimated_feature_norm: The current estimated feature norm.
    learning_rate: The step size parameter for sgd.
    key: The jax prng key.
    method: 'naive', 'lissa', or 'oracle'.
    lissa_kappa: The parameter of the lissa method, if used.
    main_batch_size: How many states to update at once.
    covariance_batch_size: the 'J' parameter. For the naive method, this is how
      many states we sample to construct the inverse. For the lissa method,
      ditto -- these are also "iterations".
    weight_batch_size: How many states to construct the weight vector.
    num_tasks: The total number of tasks.
    estimate_feature_norm: Whether to use a running average of the max feature
      norm rather than the real maximum.
    sample_states: A function that takes an rng key and a number of states
      to sample, and returns a tuple containing
      (a vector of sampled states, an updated rng key).
    use_tabular_gradient: If true, the train step will calculate the
      gradient using the tabular calculation. Otherwise, it will use a
      jax.vjp to backpropagate the gradient.

  Returns:
    A dict containing updated values for Phi, estimated_feature_norm, key,
      and optimizer_state, as well as the the computed gradient.
  """
  num_states, d = Phi.shape

  # Draw one or many source states to update, and its task.
  source_states, key = sample_states(key, main_batch_size)
  task_key, key = jax.random.split(key)
  task = jax.random.choice(task_key, num_tasks, (1,))

  # Use the source states to update our estimate of the feature norm.
  # Do this pre-LISSA, avoid a bad first gradient.
  if method == 'lissa' and estimate_feature_norm:
    features = Phi[source_states, :]
    max_norm = utils.compute_max_feature_norm(features)
    estimated_feature_norm = (
        estimated_feature_norm + 0.01 * (max_norm - estimated_feature_norm))

  ### This determines the weight vectors to be used to perform the gradient
  ### step.
  if method == 'explicit':
    # With the explicit method we maintain a running weight vector.
    # TODO(bellemare): This assumes we are sampling exactly one task. But
    # other parts of the code are actually also dependent on this point...
    weight_1 = jnp.squeeze(explicit_weight_matrix[:, task], axis=1)
    weight_2 = jnp.squeeze(explicit_weight_matrix[:, task], axis=1)
  else:  # Implicit methods.
    # Please resist the urge to refactor this code for now.
    if method == 'oracle':
      # This exactly determines the covariance.
      covariance_1 = jnp.linalg.pinv(Phi.T @ Phi) * num_states
      covariance_2 = covariance_1

      # Use all states for weight vector.
      weight_states_1 = jnp.arange(0, num_states)
      weight_states_2 = weight_states_1
    if method == 'naive':
      # The naive method uses one covariance matrix for both weight vectors.
      covariance_1, key = estimates.naive_inverse_covariance_matrix(
          Phi,
          sample_states,
          key,
          covariance_batch_size)
      covariance_2 = covariance_1

      weight_states_1, key = sample_states(key, weight_batch_size)
      weight_states_2 = weight_states_1
    elif method == 'naive++':
      # The naive method uses one covariance matrix for both weight vectors.
      covariance_1, key = estimates.naive_inverse_covariance_matrix(
          Phi,
          sample_states,
          key,
          covariance_batch_size)
      covariance_2, key = estimates.naive_inverse_covariance_matrix(
          Phi,
          sample_states,
          key,
          covariance_batch_size)

      weight_states_1, key = sample_states(key, weight_batch_size)
      weight_states_2, key = sample_states(key, weight_batch_size)
    elif method == 'lissa':
      # Compute two independent estimates of the inverse covariance matrix.
      covariance_1, key = estimates.lissa_inverse_covariance_matrix(
          Phi,
          sample_states,
          key,
          covariance_batch_size,
          lissa_kappa,
          None)
      covariance_2, key = estimates.lissa_inverse_covariance_matrix(
          Phi,
          sample_states,
          key,
          covariance_batch_size,
          lissa_kappa,
          None)

      # Draw two separate sets of states for the weight vectors (important!)
      weight_states_1, key = sample_states(key, weight_batch_size)
      weight_states_2, key = sample_states(key, weight_batch_size)

    # Compute the weight estimates by combining the inverse covariance
    # estimate and the sampled Phi & Psi's.
    weight_1 = (covariance_1 @ Phi[weight_states_1, :].T
                @ compute_psi(weight_states_1, task)) / len(weight_states_1)
    weight_2 = (covariance_2 @ Phi[weight_states_2, :].T
                @ compute_psi(weight_states_2, task)) / len(weight_states_2)

  # Compute the gradient at that source state.
  prediction = jnp.dot(Phi[source_states, :], weight_1)
  estimated_error = prediction - compute_psi(source_states, task)

  if use_tabular_gradient:
    # We use the same weight vector to move all elements of our batch, but
    # they have different errors.
    partial_gradient = jnp.reshape(
        jnp.tile(weight_2, main_batch_size), (main_batch_size, d))

    # Line up the shapes of error and weight vectors so we can construct the
    # gradient.
    expanded_estimated_error = jnp.expand_dims(estimated_error, axis=1)
    partial_gradient = partial_gradient * expanded_estimated_error

    # Note: this doesn't work for duplicate indices. However, it shouldn't
    # add any bias to the algorithm, and is faster than checking for
    # duplicate indices. Most of the case we care about the case where our
    # batch size is much smaller than the number of states, so duplicate
    # indices should be rare.
    gradient = jnp.zeros_like(Phi)
    gradient = gradient.at[source_states, :].set(partial_gradient)
  else:
    # TODO(joshgreaves): Account for batch size.
    # Note: The argument passed to vjp should be a function of parameters
    # to Phi. Currently we don't support neural networks, so we
    # include a tabular version that just passes Phi through.
    _, phi_vjp = jax.vjp(lambda Phi_: Phi_[source_states, :], Phi)
    # Calculate implicit gradient (Phi @ w_1 - Psi) @ w_2.T
    implicit_gradient = jnp.outer(estimated_error, weight_2)
    # Pullback implicit gradient to get the full Phi gradient.
    (gradient,) = phi_vjp(implicit_gradient)

  updates, optimizer_state = optimizer.update(gradient, optimizer_state)
  Phi = optax.apply_updates(Phi, updates)

  if method == 'explicit':
    # Also update the weight vector for this task.
    weight_gradient = Phi[source_states, :].T @ estimated_error
    expanded_gradient = jnp.expand_dims(weight_gradient, axis=1)
    explicit_weight_matrix = explicit_weight_matrix.at[:, task].set(
        explicit_weight_matrix[:, task] - learning_rate * expanded_gradient)

  return {
      'Phi': Phi,
      'estimated_feature_norm': estimated_feature_norm,
      'explicit_weight_matrix': explicit_weight_matrix,
      'key': key,
      'optimizer_state': optimizer_state,
      'gradient': gradient,
  }


def train(*,
          workdir,
          initial_step,
          chkpt_manager,
          Phi,
          Psi,
          optimal_subspace,
          num_epochs,
          learning_rate,
          key,
          method,
          lissa_kappa,
          optimizer,
          covariance_batch_size,
          main_batch_size,
          weight_batch_size,
          estimate_feature_norm = True,
          sample_with_replacement = True,
          use_tabular_gradient = True):
  """Training function.

  For lissa, the total number of samples is
  2 x covariance_batch_size + main_batch_size + 2 x weight_batch_size.

  Args:
    workdir: Work directory, where we'll save logs.
    initial_step: Initial step
    chkpt_manager: Checkpoint manager.
    Phi: The initial feature matrix.
    Psi: The target matrix whose PCA is to be determined.
    optimal_subspace: Top-d left singular vectors of Psi.
    num_epochs: How many gradient steps to perform. (Not really epochs)
    learning_rate: The step size parameter for sgd.
    key: The jax prng key.
    method: 'naive', 'lissa', or 'oracle'.
    lissa_kappa: The parameter of the lissa method, if used.
    optimizer: Which optimizer to use. Only 'sgd' is supported.
    covariance_batch_size: the 'J' parameter. For the naive method, this is how
      many states we sample to construct the inverse. For the lissa method,
      ditto -- these are also "iterations".
    main_batch_size: How many states to update at once.
    weight_batch_size: How many states to construct the weight vector.
    estimate_feature_norm: Whether to use a running average of the max feature
      norm rather than the real maximum.
    sample_with_replacement: Whether to draw states with replacement.
    use_tabular_gradient: If true, the train step will calculate the
      gradient using the tabular calculation. Otherwise, it will use a
      jax.vjp to backpropagate the gradient.

  Returns:
    A matrix of all Phis computed throughout training. This will be of shape
        (num_epochs, d, d).
  """
  # Don't overwrite Phi.
  Phi = jnp.copy(Phi)
  Phis = [jnp.copy(Phi)]

  _, d = Phi.shape
  _, num_tasks = Psi.shape

  # Keep a running average of the max norm of a feature vector. None means:
  # don't do it.
  if estimate_feature_norm:
    estimated_feature_norm = utils.compute_max_feature_norm(Phi)
  else:
    estimated_feature_norm = None

  # Create an explicit weight vector (needed for explicit method).
  key, weight_key = jax.random.split(key)
  explicit_weight_matrix = jax.random.normal(
      weight_key, (d, num_tasks), dtype=jnp.float64)

  if optimizer == 'sgd':
    optimizer = optax.sgd(learning_rate)
  elif optimizer == 'adam':
    optimizer = optax.adam(learning_rate)
  else:
    raise ValueError(f'Unknown optimizer {optimizer}.')
  optimizer_state = optimizer.init(Phi)

  writer = metric_writers.create_default_writer(
      logdir=str(workdir),
  )

  # Checkpointing and logging too much can use a lot of disk space.
  # Therefore, we don't want to checkpoint more than 10 times an experiment,
  # or keep more than 1k Phis per experiment.
  checkpoint_period = max(num_epochs // 10, 100_000)
  log_period = max(1_000, num_epochs // 1_000)

  hooks = [
      periodic_actions.PeriodicCallback(
          every_steps=checkpoint_period,
          callback_fn=lambda step, t: chkpt_manager.save((step, Phi)))
  ]

  # TODO(joshgreaves): Pass in num_states.
  sample_states = functools.partial(
      utils.sample_discrete_states,
      num_states=Phi.shape[0],
      sample_with_replacement=sample_with_replacement)
  # Implement the Psi mapping by indexing into the given matrix.
  compute_psi = lambda states, tasks: Psi[states, tasks]

  fixed_train_kwargs = {
      'optimizer': optimizer,
      'learning_rate': learning_rate,
      'method': method,
      'lissa_kappa': lissa_kappa,
      'main_batch_size': main_batch_size,
      'covariance_batch_size': covariance_batch_size,
      'weight_batch_size': weight_batch_size,
      'num_tasks': Psi.shape[1],  # TODO(joshgreaves): Pass in num_tasks.
      'estimate_feature_norm': estimate_feature_norm,
      'sample_states': sample_states,
      'compute_psi': compute_psi,
      'use_tabular_gradient': use_tabular_gradient,
  }
  variable_kwargs = {
      'Phi': Phi,
      'optimizer_state': optimizer_state,
      'explicit_weight_matrix': explicit_weight_matrix,  # Used by explicit.
      'estimated_feature_norm': estimated_feature_norm,
      'key': key,
  }

  # Perform num_epochs gradient steps.
  with metric_writers.ensure_flushes(writer):
    for step in etqdm.tqdm(
        range(initial_step + 1, num_epochs + 1),
        initial=initial_step,
        total=num_epochs):

      variable_kwargs = _train_step(**fixed_train_kwargs, **variable_kwargs)
      gradient = variable_kwargs.pop('gradient')

      if step % log_period == 0:
        Phi = variable_kwargs['Phi']
        Phis.append(jnp.copy(Phi))

        metrics = compute_metrics(Phi, optimal_subspace)
        metrics |= {'grad_norm': jnp.linalg.norm(gradient)}
        metrics |= {'frob_norm': utils.outer_objective_mc(Phi, Psi)}
        writer.write_scalars(step, metrics)

      for hook in hooks:
        hook(step)

  writer.flush()

  return jnp.stack(Phis)


def main(_):
  jax.config.update('jax_enable_x64', True)

  config: config_dict.ConfigDict = _CONFIG.value
  logging.info(config)

  key = jax.random.PRNGKey(config.seed)
  key, psi_key, phi_key = jax.random.split(key, 3)

  if config.use_mnist:
    Psi = utils.get_mnist_data()
  else:
    Psi = jax.random.normal(psi_key, (config.S, config.T), dtype=jnp.float64)
    if config.rescale_psi == 'linear':
      Psi = utils.generate_psi_linear(Psi)
    elif config.rescale_psi == 'exp':
      Psi = utils.generate_psi_exp(Psi)

  Phi = jax.random.normal(phi_key, (Psi.shape[0], config.d), dtype=jnp.float64)

  chkpt_manager = checkpoint.Checkpoint(base_directory=_WORKDIR.value)

  initial_step = 0
  initial_step, Phi = chkpt_manager.restore_or_initialize((initial_step, Phi))

  if config.svd_path:
    logging.info('Loading SVD from %s', config.svd_path)
    with epath.Path(config.svd_path).open('rb') as f:
      left_svd = np.load(f)
      optimal_subspace = left_svd[:, :config.d]
  else:
    optimal_subspace = compute_optimal_subspace(Psi, config.d)

  workdir = epath.Path(_WORKDIR.value)
  workdir.mkdir(exist_ok=True)

  Phis = train(
      workdir=workdir,
      initial_step=initial_step,
      chkpt_manager=chkpt_manager,
      Phi=Phi,
      Psi=Psi,
      optimal_subspace=optimal_subspace,
      num_epochs=config.num_epochs,
      learning_rate=config.lr,
      key=key,
      method=config.method,
      lissa_kappa=config.kappa,
      optimizer=config.optimizer,
      covariance_batch_size=config.covariance_batch_size,
      main_batch_size=config.main_batch_size,
      weight_batch_size=config.weight_batch_size,
      estimate_feature_norm=config.estimate_feature_norm,
      sample_with_replacement=config.sample_with_replacement,
      use_tabular_gradient=config.use_tabular_gradient)

  with (workdir / 'phis.pkl').open('wb') as fout:
    pickle.dump(Phis, fout, protocol=4)


if __name__ == '__main__':
  app.run(main)
