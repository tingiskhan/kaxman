from typing import Any, Optional, Tuple

import jax
import jax.numpy as jnp
import numpyro.distributions as dist
from jax import lax


def _inflate_missing(
    non_valid_mask: jnp.ndarray, obs: jnp.ndarray, H: jnp.ndarray, R: jnp.ndarray, missing_value: float = 1e12
) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """
    Masks missing dimensions by zeroing corresponding rows of H and inflating the diagonal of R.

    Args:
        non_valid_mask: Boolean mask of shape (obs_dim,) indicating missing dimensions.
        obs: Observation vector of shape (obs_dim,). Missing entries are NaN.
        H: Observation matrix of shape (obs_dim, state_dim).
        R: Observation covariance matrix of shape (obs_dim, obs_dim).
        missing_value: Large scalar to add on the diagonal for missing dimensions.

    Returns:
        A tuple of:
          - obs_masked: Same shape as obs, with missing entries replaced by 0.0.
          - H_masked: Same shape as H, rows zeroed out for missing dimensions.
          - R_masked: Same shape as R, diagonal entries inflated for missing dimensions.
    """

    valid_mask = ~non_valid_mask
    valid_mask_f = valid_mask.astype(obs.dtype)

    H_masked = H * valid_mask_f[:, None]
    diag_inflation = (1.0 - valid_mask_f) * missing_value
    R_masked = R + jnp.diag(diag_inflation)
    obs_masked = jnp.where(valid_mask, obs, 0.0)

    return obs_masked, H_masked, R_masked


class KalmanFilter:
    """
    A JAX-based Kalman Filter supporting partial missing data, offsets, optional noise transform,
    integrated log-likelihood, and RTS smoothing. Uses pseudo-inverse to handle degenerate covariances.
    """

    def __init__(
        self,
        initial_mean: jnp.ndarray,
        initial_cov: jnp.ndarray,
        transition_matrix: Any,
        transition_cov: Any,
        observation_matrix: Any,
        observation_cov: Any,
        transition_offset: Optional[Any] = None,
        observation_offset: Optional[Any] = None,
        noise_transform: Optional[Any] = None,
    ):
        """
        Initializes a KalmanFilter.

        Args:
            initial_mean: Mean of the initial state, shape (state_dim,).
            initial_cov: Covariance of the initial state, shape (state_dim, state_dim).
            transition_matrix: F_t, shape (state_dim, state_dim) or callable returning it.
            transition_cov: Q_t, shape (noise_dim, noise_dim) or callable returning it.
            observation_matrix: H_t, shape (obs_dim, state_dim) or callable returning it.
            observation_cov: R_t, shape (obs_dim, obs_dim) or callable returning it.
            transition_offset: b_t, shape (state_dim,) or callable returning it. Default is None.
            observation_offset: d_t, shape (obs_dim,) or callable returning it. Default is None.
            noise_transform: G_t, shape (state_dim, noise_dim) or callable returning it.
                If None, an identity matrix of size (state_dim, state_dim) is used.
        """

        self.initial_mean = initial_mean
        self.initial_cov = initial_cov

        self.transition_matrix = transition_matrix
        self.transition_cov = transition_cov
        self.observation_matrix = observation_matrix
        self.observation_cov = observation_cov

        self.transition_offset = transition_offset
        self.observation_offset = observation_offset

        if noise_transform is None:
            st_dim = initial_mean.shape[0]
            noise_transform = jnp.eye(st_dim)
        self.noise_transform = noise_transform

    def _get_transition_matrix(self, t: int, x_prev: jnp.ndarray) -> jnp.ndarray:
        if callable(self.transition_matrix):
            return self.transition_matrix(t, x_prev)
        return self.transition_matrix

    def _get_transition_cov(self, t: int) -> jnp.ndarray:
        if callable(self.transition_cov):
            return self.transition_cov(t)
        return self.transition_cov

    def _get_observation_matrix(self, t: int) -> jnp.ndarray:
        if callable(self.observation_matrix):
            return self.observation_matrix(t)
        return self.observation_matrix

    def _get_observation_cov(self, t: int) -> jnp.ndarray:
        if callable(self.observation_cov):
            return self.observation_cov(t)
        return self.observation_cov

    def _get_transition_offset(self, t: int) -> jnp.ndarray:
        if self.transition_offset is None:
            return 0.0
        if callable(self.transition_offset):
            return self.transition_offset(t)
        return self.transition_offset

    def _get_observation_offset(self, t: int) -> jnp.ndarray:
        if self.observation_offset is None:
            return 0.0
        if callable(self.observation_offset):
            return self.observation_offset(t)
        return self.observation_offset

    def _get_noise_transform(self, t: int) -> jnp.ndarray:
        if callable(self.noise_transform):
            return self.noise_transform(t)
        return self.noise_transform

    def _predict(self, mean: jnp.ndarray, cov: jnp.ndarray, t: int) -> Tuple[jnp.ndarray, jnp.ndarray]:
        F_t = self._get_transition_matrix(t, mean)
        Q_t = self._get_transition_cov(t)
        b_t = self._get_transition_offset(t)
        G_t = self._get_noise_transform(t)

        mean_pred = F_t @ mean + b_t
        cov_pred = F_t @ cov @ F_t.T + G_t @ Q_t @ G_t.T

        return mean_pred, cov_pred

    def _update(
        self, mean_pred: jnp.ndarray, cov_pred: jnp.ndarray, obs: jnp.ndarray, t: int, missing_value: float
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        H_t = self._get_observation_matrix(t)
        R_t = self._get_observation_cov(t)

        mask = jnp.isnan(obs)
        obs_masked, H_masked, R_masked = _inflate_missing(mask, obs, H_t, R_t, missing_value)

        S = H_masked @ cov_pred @ H_masked.T + R_masked
        S_pinv = jnp.linalg.pinv(S)

        K = cov_pred @ H_masked.T @ S_pinv
        residual = obs_masked - (H_masked @ mean_pred)

        mean_up = mean_pred + K @ residual
        cov_up = cov_pred - K @ H_masked @ cov_pred

        return mean_up, cov_up

    def _forward_pass(
        self, observations: jnp.ndarray, missing_value: float
    ) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
        def scan_fn(carry, obs_t):
            t, mean_t, cov_t, ll_so_far = carry

            mean_pred, cov_pred = self._predict(mean_t, cov_t, t)

            H_t = self._get_observation_matrix(t)
            R_t = self._get_observation_cov(t)
            d_t = self._get_observation_offset(t)

            obs_mask = jnp.isnan(obs_t)
            obs_masked, H_masked, R_masked = _inflate_missing(obs_mask, obs_t, H_t, R_t, missing_value)

            pred_mean_masked = H_masked @ mean_pred + jnp.where(jnp.isnan(obs_t), 0.0, d_t)
            pred_cov_masked = H_masked @ cov_pred @ H_masked.T + R_masked

            dist_y = dist.MultivariateNormal(loc=pred_mean_masked, covariance_matrix=pred_cov_masked)
            step_log_prob = dist_y.log_prob(obs_masked)
            new_ll_so_far = ll_so_far + step_log_prob

            mean_up, cov_up = self._update(mean_pred, cov_pred, obs_t, t, missing_value)

            return (t + 1, mean_up, cov_up, new_ll_so_far), (mean_pred, cov_pred, mean_up, cov_up)

        init_carry = (0, self.initial_mean, self.initial_cov, 0.0)
        final_carry, outputs = lax.scan(scan_fn, init_carry, observations)

        _, _, _, total_ll = final_carry
        predicted_means = outputs[0]
        predicted_covs = outputs[1]
        filtered_means = outputs[2]
        filtered_covs = outputs[3]

        return predicted_means, predicted_covs, filtered_means, filtered_covs, total_ll

    def filter(
        self, observations: jnp.ndarray, missing_value: float = 1e12
    ) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
        (_pred_means, _pred_covs, filtered_means, filtered_covs, total_ll) = self._forward_pass(
            observations, missing_value
        )

        return filtered_means, filtered_covs, total_ll

    def smooth(
        self, observations: jnp.ndarray, missing_value: float = 1e12
    ) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
        (predicted_means, predicted_covs, filtered_means, filtered_covs, total_ll) = self._forward_pass(
            observations, missing_value
        )

        num_timesteps = observations.shape[0]
        state_dim = self.initial_mean.shape[0]

        smoothed_means = jnp.zeros((num_timesteps, state_dim))
        smoothed_covs = jnp.zeros((num_timesteps, state_dim, state_dim))

        smoothed_means = smoothed_means.at[-1].set(filtered_means[-1])
        smoothed_covs = smoothed_covs.at[-1].set(filtered_covs[-1])

        def rts_loop_body(i, carry):
            next_mean_smooth, next_cov_smooth = carry

            mean_f = filtered_means[i]
            cov_f = filtered_covs[i]
            mean_p = predicted_means[i + 1]
            cov_p = predicted_covs[i + 1]

            F_t = self._get_transition_matrix(i + 1, mean_f)

            cov_p_inv = jnp.linalg.pinv(cov_p)
            A_t = cov_f @ F_t.T @ cov_p_inv

            curr_mean_smooth = mean_f + A_t @ (next_mean_smooth - mean_p)
            curr_cov_smooth = cov_f + A_t @ (next_cov_smooth - cov_p) @ A_t.T

            smoothed_means_ = smoothed_means.at[i].set(curr_mean_smooth)
            smoothed_covs_ = smoothed_covs.at[i].set(curr_cov_smooth)

            return (curr_mean_smooth, curr_cov_smooth), (smoothed_means_, smoothed_covs_)

        init_carry = (filtered_means[-1], filtered_covs[-1])
        indices = jnp.arange(num_timesteps - 1)[::-1]

        def outer_scan(carry, i):
            (curr_mean_smooth, curr_cov_smooth), (smeans, scovs) = rts_loop_body(i, carry)
            return (curr_mean_smooth, curr_cov_smooth), (smeans, scovs)

        (_, _), (final_smeans, final_scovs) = lax.scan(outer_scan, init_carry, indices)

        smoothed_means = final_smeans
        smoothed_covs = final_scovs

        return smoothed_means, smoothed_covs, total_ll

    def sample(self, rng_key: jax.random.PRNGKey, num_timesteps: int) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Samples from the state-space model for num_timesteps.
        """

        def sample_step(carry, _):
            t, x_prev = carry

            F_t = self._get_transition_matrix(t, x_prev)
            Q_t = self._get_transition_cov(t)
            b_t = self._get_transition_offset(t)
            G_t = self._get_noise_transform(t)
            H_t = self._get_observation_matrix(t)
            R_t = self._get_observation_cov(t)
            d_t = self._get_observation_offset(t)

            rng_proc, rng_obs = jax.random.split(rng_key)
            noise_dim = Q_t.shape[0]
            w_t = dist.MultivariateNormal(loc=jnp.zeros(noise_dim), covariance_matrix=Q_t).sample(rng_proc)

            x_t = F_t @ x_prev + b_t + G_t @ w_t
            obs_dim = R_t.shape[0]
            v_t = dist.MultivariateNormal(loc=jnp.zeros(obs_dim), covariance_matrix=R_t).sample(rng_obs)

            y_t = H_t @ x_t + d_t + v_t

            return (t + 1, x_t), (x_t, y_t)

        def init_state(key):
            return dist.MultivariateNormal(loc=self.initial_mean, covariance_matrix=self.initial_cov).sample(key)

        x0 = init_state(rng_key)
        init_carry = (0, x0)

        _, (xs, ys) = lax.scan(sample_step, init_carry, None, length=num_timesteps)

        return xs, ys
