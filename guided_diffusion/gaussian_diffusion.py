import math
import os
from os.path import join as pjoin
from functools import partial
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
import torch
from tqdm.auto import tqdm

from torchvision.utils import make_grid
from dps.util.img_utils import clear_color
from .posterior_mean_variance import get_mean_processor, get_var_processor

import osmosis_utils.utils as utilso

__SAMPLER__ = {}


def register_sampler(name: str):
    def wrapper(cls):
        if __SAMPLER__.get(name, None):
            raise NameError(f"Name {name} is already registered!")
        __SAMPLER__[name] = cls
        return cls

    return wrapper


def get_sampler(name: str):
    if __SAMPLER__.get(name, None) is None:
        raise NameError(f"Name {name} is not defined!")
    return __SAMPLER__[name]


def create_sampler(sampler,
                   steps,
                   noise_schedule,
                   model_mean_type,
                   model_var_type,
                   dynamic_threshold,
                   clip_denoised,
                   rescale_timesteps,
                   timestep_respacing="",
                   **kwargs):
    sampler = get_sampler(name=sampler)

    annealing_time = kwargs.get('annealing_time', False)
    betas = get_named_beta_schedule(noise_schedule, steps)
    if not timestep_respacing:
        timestep_respacing = [steps]

    return sampler(use_timesteps=space_timesteps(steps, timestep_respacing),
                   betas=betas,
                   model_mean_type=model_mean_type,
                   model_var_type=model_var_type,
                   dynamic_threshold=dynamic_threshold,
                   clip_denoised=clip_denoised,
                   rescale_timesteps=rescale_timesteps,
                   annealing_time=annealing_time)


class GaussianDiffusion:
    def __init__(self,
                 betas,
                 model_mean_type,
                 model_var_type,
                 dynamic_threshold,
                 clip_denoised,
                 rescale_timesteps,
                 **kwargs):

        self.annealing_time = kwargs.get("annealing_time", False)
        self.min_max_denoised = kwargs.get("min_max_denoised", False)
        # use float64 for accuracy.
        betas = np.array(betas, dtype=np.float64)
        self.betas = betas
        assert self.betas.ndim == 1, "betas must be 1-D"
        assert (0 < self.betas).all() and (self.betas <= 1).all(), "betas must be in (0..1]"

        self.num_timesteps = int(self.betas.shape[0])
        self.rescale_timesteps = rescale_timesteps

        alphas = 1.0 - self.betas
        self.alphas_cumprod = np.cumprod(alphas, axis=0)
        self.alphas_cumprod_prev = np.append(1.0, self.alphas_cumprod[:-1])
        self.alphas_cumprod_next = np.append(self.alphas_cumprod[1:], 0.0)
        assert self.alphas_cumprod_prev.shape == (self.num_timesteps,)

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.sqrt_alphas_cumprod = np.sqrt(self.alphas_cumprod)
        self.sqrt_one_minus_alphas_cumprod = np.sqrt(1.0 - self.alphas_cumprod)
        self.log_one_minus_alphas_cumprod = np.log(1.0 - self.alphas_cumprod)
        self.sqrt_recip_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod)
        self.sqrt_recipm1_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod - 1)

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        self.posterior_variance = (
                betas * (1.0 - self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        # log calculation clipped because the posterior variance is 0 at the
        # beginning of the diffusion chain.
        self.posterior_log_variance_clipped = np.log(
            np.append(self.posterior_variance[1], self.posterior_variance[1:])
        )
        self.posterior_mean_coef1 = (
                betas * np.sqrt(self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        self.posterior_mean_coef2 = (
                (1.0 - self.alphas_cumprod_prev)
                * np.sqrt(alphas)
                / (1.0 - self.alphas_cumprod)
        )

        self.mean_processor = get_mean_processor(model_mean_type,
                                                 betas=betas,
                                                 dynamic_threshold=dynamic_threshold,
                                                 clip_denoised=clip_denoised)

        self.var_processor = get_var_processor(model_var_type,
                                               betas=betas)

    def q_mean_variance(self, x_start, t):
        """
        Get the distribution q(x_t | x_0).

        :param x_start: the [N x C x ...] tensor of noiseless inputs.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :return: A tuple (mean, variance, log_variance), all of x_start's shape.
        """

        mean = extract_and_expand(self.sqrt_alphas_cumprod, t, x_start) * x_start
        variance = extract_and_expand(1.0 - self.alphas_cumprod, t, x_start)
        log_variance = extract_and_expand(self.log_one_minus_alphas_cumprod, t, x_start)

        return mean, variance, log_variance

    def q_sample(self, x_start, t):
        """
        Diffuse the data for a given number of diffusion steps.

        In other words, sample from q(x_t | x_0).

        :param x_start: the initial data batch.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :param noise: if specified, the split-out normal noise.
        :return: A noisy version of x_start.
        """
        noise = torch.randn_like(x_start)
        assert noise.shape == x_start.shape

        coef1 = extract_and_expand(self.sqrt_alphas_cumprod, t, x_start)
        coef2 = extract_and_expand(self.sqrt_one_minus_alphas_cumprod, t, x_start)

        return coef1 * x_start + coef2 * noise

    def q_posterior_mean_variance(self, x_start, x_t, t):
        """
        Compute the mean and variance of the diffusion posterior:

            q(x_{t-1} | x_t, x_0)

        """
        assert x_start.shape == x_t.shape
        coef1 = extract_and_expand(self.posterior_mean_coef1, t, x_start)
        coef2 = extract_and_expand(self.posterior_mean_coef2, t, x_t)
        posterior_mean = coef1 * x_start + coef2 * x_t
        posterior_variance = extract_and_expand(self.posterior_variance, t, x_t)
        posterior_log_variance_clipped = extract_and_expand(self.posterior_log_variance_clipped, t, x_t)

        assert (
                posterior_mean.shape[0]
                == posterior_variance.shape[0]
                == posterior_log_variance_clipped.shape[0]
                == x_start.shape[0]
        )
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def p_sample_loop(self,
                      model,
                      x_start,
                      measurement,
                      measurement_cond_fn,
                      record,
                      save_root,
                      pretrain_model=None,
                      image_idx=None,
                      record_every=150,
                      check_prior=False,
                      sample_pattern=None,
                      **kwargs):
        """
        The function used for sampling from noise.
        """

        img = x_start
        device = x_start.device
        guide_and_sample = kwargs.get("guide_and_sample", False)
        global_iteration = kwargs.get("global_iteration", False)

        if record:
            n_images = img.shape[0]
            rgb_record_dict = {ii: [] for ii in range(n_images)}
            depth_record_dict = {ii: [] for ii in range(n_images)}
            histogram_record_dict = {ii: [] for ii in range(n_images)}

            rgb_record_dict_mm = {ii: [] for ii in range(n_images)}
            depth_record_dict_mm = {ii: [] for ii in range(n_images)}
            histogram_record_dict_mm = {ii: [] for ii in range(n_images)}

            gradients_record_dict = {ii: [] for ii in range(n_images)}

        loss_process = []
        factor = 1
        steps = self.num_timesteps
        total_steps = factor * self.num_timesteps
        pbar = tqdm(list(range(total_steps))[::-1])

        time_val_list = []

        # loop over the timestep
        for idx in pbar:

            # if idx == 1 or idx== 0:
            #     print ("osmosis_utils")


            # the annealing option
            if self.annealing_time:

                # Linearly decreasing + cosine
                idx = idx / float(factor)
                t = (idx + idx // 4 * math.cos((steps - idx) / 50)) / steps * 1000
                # t = (idx + idx // 3 * math.cos((steps - idx) / 80 + 1.5)) / steps * 1000

                # TODO short version for less samples - good for 500 - does not work for now
                # t = (idx / 1.5 + idx / 3 * math.cos((steps-idx) / 30)) / steps * 800

                # clamp t between [1,1000]
                t = torch.tensor([t], dtype=torch.int64, device=device, requires_grad=False)
                time = torch.clamp(t, 1, 1000) - 1

                # in this case the idx is future for the start-stop optimization process
                idx = int(idx)

                # Sawtooth wave
                # used instead on repeat the process again from large T. it should be the same - hopfully
                # t = idx % 1000
                # future for the start-stop optimization process - here the index will be the time
                # idx = t
                # t = torch.tensor([t], dtype=torch.int64, device=device, requires_grad=False)
                # clamp t between [1,1000]
                # time = torch.clamp(t, 1, 1000) - 1

            # the original time step
            else:
                time = torch.tensor([idx] * img.shape[0], device=device)

            time_val_list.append(time.cpu().item())

            # flag (bool) for non guidance
            guidance_flag = (sample_pattern['pattern'] == 'original') or \
                            (sample_pattern['pattern'] is None) or \
                            (sample_pattern['start_guidance'] * self.num_timesteps >= time >= sample_pattern[
                                'stop_guidance'] * self.num_timesteps)

            # setting the alternate len (M from the gibbsDDRM paper or N from PGDiff paper)
            alternate_len = utilso.set_alternate_length(sample_pattern, idx, self.num_timesteps)
            for alternate_ii in range(alternate_len):

                img.requires_grad = True if guidance_flag else False

                # in DPS they first sample and then guide, in GDP they first guide and then sample
                if guide_and_sample:
                    # first guide and then sample - like GDP
                    out = self.p_mean_variance(model=model, x=img, t=time)
                    out['sample'] = out['mean']
                    # calculating the noise for future calculations
                    current_additional_noise = torch.randn_like(out['mean'], device=img.device)
                    sample_added_noise = current_additional_noise * torch.exp(0.5 * out['log_variance'])
                else:
                    # first sample new x, and then guide the sample - like DPS
                    out = self.p_sample(x=img, t=time, model=model)
                    sample_added_noise = None

                # there is no use of the noisy measurement, do we need it? I don't know yet
                noisy_measurement = self.q_sample(measurement, t=time)

                # Give condition. -> guiding
                if pretrain_model == 'debka' and not check_prior:

                    # check if there is a sampling method and check the idx to check if to freeze phi (beta and b_inf)
                    freeze_phi = utilso.is_freeze_phi(sample_pattern, idx, self.num_timesteps)

                    if guidance_flag:
                        # conditioning function
                        # img, loss, beta, b_inf, gradients
                        img, loss, variable_dict, gradients, quality_loss = measurement_cond_fn(x_t=out['sample'],
                                                                                                measurement=measurement,
                                                                                                noisy_measurement=noisy_measurement,
                                                                                                x_prev=img,
                                                                                                x_0_hat=out[
                                                                                                    'pred_xstart'],
                                                                                                freeze_phi=freeze_phi,
                                                                                                sample_added_noise=sample_added_noise,
                                                                                                time_index=float(
                                                                                                    idx) / self.num_timesteps)

                        # debug osmosis_utils - print information on the gradients
                        text_tmp = f"\nRed Gradients: min: {np.round([gradients[0, 0, :, :].min()], decimals=4)}, " \
                                   f"max: {np.round([gradients[0, 0, :, :].max()], decimals=4)}, mean: {np.round([gradients[0, 0, :, :].mean()], decimals=4)}, " \
                                   f"std: {np.round([gradients[0, 0, :, :].std()], decimals=4)}" \
                                   f"\nGreen Gradients: min: {np.round([gradients[0, 1, :, :].min()], decimals=4)}, " \
                                   f"max: {np.round([gradients[0, 1, :, :].max()], decimals=4)}, mean: {np.round([gradients[0, 1, :, :].mean()], decimals=4)}, " \
                                   f"std: {np.round([gradients[0, 1, :, :].std()], decimals=4)}" \
                                   f"\nBlue Gradients: min: {np.round([gradients[0, 2, :, :].min()], decimals=4)}, " \
                                   f"max: {np.round([gradients[0, 2, :, :].max()], decimals=4)}, mean: {np.round([gradients[0, 2, :, :].mean()], decimals=4)}, " \
                                   f"std: {np.round([gradients[0, 2, :, :].std()], decimals=4)}" \
                                   f"\nDepth Gradients: min: {np.round([gradients[0, 3, :, :].min()], decimals=4)}, " \
                                   f"max: {np.round([gradients[0, 3, :, :].max()], decimals=4)}, mean: {np.round([gradients[0, 3, :, :].mean()], decimals=4)}, " \
                                   f"std: {np.round([gradients[0, 3, :, :].std()], decimals=4)}\n"
                        # print(text_tmp)


                    else:
                        # no guidance
                        img = out['sample']

                    # in DPS they first sample and then guide, in GDP they first guide and then sample
                    if guide_and_sample:

                        # img_mean_values = img.detach().mean(dim=(2, 3), keepdim=True) * \
                        #                   torch.tensor([1, 1, 1, 0], device=img.device)[None, ..., None, None]
                        # print(img_mean_values.cpu().squeeze())

                        noise = torch.randn_like(img, device=img.device)
                        if time != 0:  # no noise when t == 0
                            # img += torch.exp(0.5 * out['log_variance']) * noise - img_mean_values
                            img += torch.exp(0.5 * out['log_variance']) * noise

                    # detach result from graph, for the next iteration
                    img.detach_()

                    # get the last iterate results
                    if alternate_ii == (alternate_len - 1):

                        loss_process.append(loss[0].item())
                        # print and log values
                        pbar_print_dictionary = {}
                        pbar_print_dictionary['time'] = time.cpu().tolist()
                        pbar_print_dictionary['loss'] = loss
                        if quality_loss is not None:
                            pbar_print_dictionary['quality'] = np.round(
                                [ii.item() for ii in list(quality_loss.values())], decimals=4)
                        for key_ii, value_ii in variable_dict.items():
                            current_var_value = np.round(value_ii.cpu().detach().squeeze().tolist(), decimals=3)
                            # in case the variable is a matrix
                            if len(current_var_value.shape) > 1:
                                current_var_value = np.round([current_var_value.mean(), current_var_value.std()],
                                                             decimals=3)
                            pbar_print_dictionary[key_ii] = current_var_value

                        pbar.set_postfix(pbar_print_dictionary, refresh=False)

                        # # prepare printings and logging
                        # print_b_inf = [np.round(i, decimals=3) for i in b_inf.detach().cpu().squeeze().tolist()]
                        # # check what is beta - if tensor - it is a non-revised uw model
                        # if isinstance(beta, torch.Tensor):
                        #     print_beta = [np.round(i, decimals=3) for i in
                        #                   beta.detach().cpu().squeeze().squeeze().squeeze().tolist()]
                        #     pbar.set_postfix({'loss': loss.item(),
                        #                       'beta': print_beta,
                        #                       'b_inf': print_b_inf}, refresh=False)
                        # # if list - it is a revised uw model and include beta a and beta b
                        # elif len(beta) == 2:
                        #     print_beta_a = [np.round(i, decimals=3) for i in beta[0].detach().cpu().squeeze().tolist()]
                        #     print_beta_b = [np.round(i, decimals=3) for i in beta[1].detach().cpu().squeeze().tolist()]
                        #     pbar.set_postfix({'loss': loss,
                        #                       'beta_a': print_beta_a,
                        #                       'beta_b': print_beta_b,
                        #                       'b_inf': print_b_inf}, refresh=False)
                        # else:
                        #     ValueError("Not recognized beta")

                # almost original dps code
                else:
                    img, loss = measurement_cond_fn(x_t=out['sample'],
                                                    measurement=measurement,
                                                    noisy_measurement=noisy_measurement,
                                                    x_prev=img,
                                                    x_0_hat=out['pred_xstart'])
                    img = img.detach_()
                    pbar.set_postfix({'loss': loss.detach().cpu().item()}, refresh=False)

                # save the images during the diffusion process
                if record and (alternate_ii == (alternate_len - 1)) and ((idx % record_every == 0) or (idx == 1) or (idx == 999)):

                    # the RGBD image
                    mid_x_0_pred_tmp = out['pred_xstart'].detach().cpu()

                    # split into RGB and D images
                    rgb_record_tmp = 0.5 * (mid_x_0_pred_tmp[:, 0:3, :, :] + 1)
                    rgb_record_tmp_clip = torch.clamp(rgb_record_tmp, 0, 1)
                    rgb_record_tmp_mm = [utilso.min_max_norm_range(rgb_record_tmp[ii]) for ii in range(n_images)]

                    depth_record_tmp = mid_x_0_pred_tmp[:, 3, :, :]

                    # percentile + min max norm for the depth image
                    depth_record_tmp_percentile_mm = [utilso.min_max_norm_range_percentile(
                        depth_record_tmp[ii, :, :].unsqueeze(0), percent_low=0.05, percent_high=0.99).repeat(3, 1, 1) for
                                                      ii in range(n_images)]

                    # min max norm for the depth image
                    depth_record_tmp_mm = [utilso.min_max_norm_range(
                        depth_record_tmp[ii, :, :].unsqueeze(0)).repeat(3, 1, 1) for ii in range(n_images)]

                    if pretrain_model == 'debka' and not check_prior:
                        gradients_record_tmp = [utilso.min_max_norm_range(
                            gradients[ii, :, :, :].abs()) for ii in range(n_images)]

                    for key_ii in rgb_record_dict:

                        # rgb: clip [0,1] after 0.5*(x+1), depth:  min max norm
                        rgb_record_dict[key_ii].append(rgb_record_tmp_clip[key_ii])
                        depth_record_dict[key_ii].append(depth_record_tmp_mm[key_ii])
                        histogram_tensor = utilso.color_histogram(rgb_record_tmp_clip[key_ii], title=idx)
                        histogram_record_dict[key_ii].append(histogram_tensor)

                        # rgb: min-max norm, depth: percentile + min-max norm
                        rgb_record_dict_mm[key_ii].append(rgb_record_tmp_mm[key_ii])
                        depth_record_dict_mm[key_ii].append(depth_record_tmp_percentile_mm[key_ii])
                        histogram_tensor_mm = utilso.color_histogram(rgb_record_tmp_mm[key_ii],
                                                                     title=f"{idx} "
                                                                           f"[{np.round([rgb_record_tmp[key_ii].min()], decimals=2)},"
                                                                           f"{np.round([rgb_record_tmp[key_ii].max()], decimals=2)}]")
                        histogram_record_dict_mm[key_ii].append(histogram_tensor_mm)

                        if pretrain_model == 'debka' and not check_prior:
                            gradients_record_dict[key_ii].append(gradients_record_tmp[key_ii])

        # save the recorded images
        if record:

            for key_ii in rgb_record_dict:
                # save rgb and depth information - images are clipped, depth is percentiled + min-max normalized
                mid_im = make_grid(rgb_record_dict[key_ii] + depth_record_dict[key_ii] + histogram_record_dict[key_ii],
                                   nrow=len(rgb_record_dict[key_ii]))
                mid_im = utilso.clip_image(mid_im, scale=False, move=False, is_uint8=True).permute(1, 2, 0).numpy()
                mid_im_pil = Image.fromarray(mid_im, mode="RGB")
                # save the image
                mid_im_pil.save(pjoin(save_root, f'image_{image_idx}_{key_ii}_process_g{global_iteration}.png'))

                # save rgb and depth information - images and depth are min max normalized
                mid_im = make_grid(
                    rgb_record_dict_mm[key_ii] + depth_record_dict_mm[key_ii] + histogram_record_dict_mm[key_ii],
                    nrow=len(rgb_record_dict[key_ii]))
                mid_im = utilso.clip_image(mid_im, scale=False, move=False, is_uint8=True).permute(1, 2, 0).numpy()
                mid_im_pil = Image.fromarray(mid_im, mode="RGB")
                # save the image
                mid_im_pil.save(pjoin(save_root, f'image_{image_idx}_{key_ii}_process_g{global_iteration}_mm.png'))

                # visualization of gradients information
                # gradient_grid_list = []
                # for grad_ii in gradients_record_dict[key_ii]:
                #     gradient_grid_list_tmp = [grad_ii[0].unsqueeze(0).repeat(3, 1, 1),
                #                               grad_ii[1].unsqueeze(0).repeat(3, 1, 1),
                #                               grad_ii[2].unsqueeze(0).repeat(3, 1, 1),
                #                               grad_ii[0:3],
                #                               grad_ii[3].unsqueeze(0).repeat(3, 1, 1)]
                #
                #     gradient_grid_list += gradient_grid_list_tmp
                # mid_gradients = make_grid(gradient_grid_list, nrow=5, pad_value=1.0)
                # mid_gradients = utilso.clip_image(mid_gradients, scale=False, move=False,
                #                                   is_uint8=True).permute(1, 2, 0).numpy()
                # mid_gradients_pil = Image.fromarray(mid_gradients, mode="RGB")
                # # save the image
                # mid_gradients_pil.save(pjoin(save_root, f'image_{image_idx}_{key_ii}_gradients.png'))

        # return the relevant things
        if pretrain_model == 'debka' and not check_prior:

            # save the losses plot
            fig, ax = plt.subplots(nrows=1, ncols=1)  # create figure & 1 axis
            ax.plot(list(np.arange(factor * self.num_timesteps)), loss_process)
            ax.set_xlabel('Diffusion time steps')
            ax.set_ylabel('Loss')
            ax.set_title(f"loss vs. Timesteps - image {image_idx}")

            fig.savefig(
                pjoin(save_root, f'image_{image_idx}_0_loss_g{global_iteration}.png'))  # save the figure to file
            plt.close(fig)  # close the figure window

            # save the time plot - interesting when doing an annealing time
            if self.annealing_time:
                fig, ax = plt.subplots(nrows=1, ncols=1)  # create figure & 1 axis
                ax.plot(list(np.arange(total_steps)), time_val_list)
                ax.set_xlabel('Diffusion time steps')
                ax.set_ylabel('Value of time')
                ax.set_title(f"Time Scheduler")

                fig.savefig(pjoin(save_root,
                                  f'image_{image_idx}_0_time_scheduler_g{global_iteration}.png'))  # save the figure to file
                plt.close(fig)  # close the figure window

            return img, variable_dict, loss, out['pred_xstart'].detach().cpu()

        else:
            return img

    def p_sample(self, model, x, t):
        raise NotImplementedError

    def p_mean_variance(self, model, x, t):
        model_output = model(x, self._scale_timesteps(t))

        # In the case of "learned" variance, model will give twice channels.
        if model_output.shape[1] == 2 * x.shape[1]:
            model_output, model_var_values = torch.split(model_output, x.shape[1], dim=1)
        else:
            # The name of variable is wrong. 
            # This will just provide shape information, and 
            # will not be used for calculating something important in variance.
            model_var_values = model_output

        model_mean, pred_xstart = self.mean_processor.get_mean_and_xstart(x, t, model_output)
        model_variance, model_log_variance = self.var_processor.get_variance(model_var_values, t)

        assert model_mean.shape == model_log_variance.shape == pred_xstart.shape == x.shape

        return {'mean': model_mean,
                'variance': model_variance,
                'log_variance': model_log_variance,
                'pred_xstart': pred_xstart}

    def _scale_timesteps(self, t):
        if self.rescale_timesteps:
            return t.float() * (1000.0 / self.num_timesteps)
        return t


def space_timesteps(num_timesteps, section_counts):
    """
    Create a list of timesteps to use from an original diffusion process,
    given the number of timesteps we want to take from equally-sized portions
    of the original process.
    For example, if there's 300 timesteps and the section counts are [10,15,20]
    then the first 100 timesteps are strided to be 10 timesteps, the second 100
    are strided to be 15 timesteps, and the final 100 are strided to be 20.
    If the stride is a string starting with "ddim", then the fixed striding
    from the DDIM paper is used, and only one section is allowed.
    :param num_timesteps: the number of diffusion steps in the original
                          process to divide up.
    :param section_counts: either a list of numbers, or a string containing
                           comma-separated numbers, indicating the step count
                           per section. As a special case, use "ddimN" where N
                           is a number of steps to use the striding from the
                           DDIM paper.
    :return: a set of diffusion steps from the original process to use.
    """
    if isinstance(section_counts, str):
        if section_counts.startswith("ddim"):
            desired_count = int(section_counts[len("ddim"):])
            for i in range(1, num_timesteps):
                if len(range(0, num_timesteps, i)) == desired_count:
                    return set(range(0, num_timesteps, i))
            raise ValueError(
                f"cannot create exactly {num_timesteps} steps with an integer stride"
            )
        section_counts = [int(x) for x in section_counts.split(",")]
    elif isinstance(section_counts, int):
        section_counts = [section_counts]

    size_per = num_timesteps // len(section_counts)
    extra = num_timesteps % len(section_counts)
    start_idx = 0
    all_steps = []
    for i, section_count in enumerate(section_counts):
        size = size_per + (1 if i < extra else 0)
        if size < section_count:
            raise ValueError(
                f"cannot divide section of {size} steps into {section_count}"
            )
        if section_count <= 1:
            frac_stride = 1
        else:
            frac_stride = (size - 1) / (section_count - 1)
        cur_idx = 0.0
        taken_steps = []
        for _ in range(section_count):
            taken_steps.append(start_idx + round(cur_idx))
            cur_idx += frac_stride
        all_steps += taken_steps
        start_idx += size
    return set(all_steps)


class SpacedDiffusion(GaussianDiffusion):
    """
    A diffusion process which can skip steps in a base diffusion process.
    :param use_timesteps: a collection (sequence or set) of timesteps from the
                          original diffusion process to retain.
    :param kwargs: the kwargs to create the base diffusion process.
    """

    def __init__(self, use_timesteps, **kwargs):
        self.use_timesteps = set(use_timesteps)
        self.timestep_map = []
        self.original_num_steps = len(kwargs["betas"])

        base_diffusion = GaussianDiffusion(**kwargs)  # pylint: disable=missing-kwoa
        last_alpha_cumprod = 1.0
        new_betas = []
        for i, alpha_cumprod in enumerate(base_diffusion.alphas_cumprod):
            if i in self.use_timesteps:
                new_betas.append(1 - alpha_cumprod / last_alpha_cumprod)
                last_alpha_cumprod = alpha_cumprod
                self.timestep_map.append(i)
        kwargs["betas"] = np.array(new_betas)
        super().__init__(**kwargs)

    def p_mean_variance(self, model, *args, **kwargs):  # pylint: disable=signature-differs
        return super().p_mean_variance(self._wrap_model(model), *args, **kwargs)

    def training_losses(self, model, *args, **kwargs):  # pylint: disable=signature-differs
        return super().training_losses(self._wrap_model(model), *args, **kwargs)

    def condition_mean(self, cond_fn, *args, **kwargs):
        return super().condition_mean(self._wrap_model(cond_fn), *args, **kwargs)

    def condition_score(self, cond_fn, *args, **kwargs):
        return super().condition_score(self._wrap_model(cond_fn), *args, **kwargs)

    def _wrap_model(self, model):
        if isinstance(model, _WrappedModel):
            return model
        return _WrappedModel(
            model, self.timestep_map, self.rescale_timesteps, self.original_num_steps
        )

    def _scale_timesteps(self, t):
        # Scaling is done by the wrapped model.
        return t


class _WrappedModel:
    def __init__(self, model, timestep_map, rescale_timesteps, original_num_steps):
        self.model = model
        self.timestep_map = timestep_map
        self.rescale_timesteps = rescale_timesteps
        self.original_num_steps = original_num_steps

    def __call__(self, x, ts, **kwargs):
        map_tensor = torch.tensor(self.timestep_map, device=ts.device, dtype=ts.dtype)
        new_ts = map_tensor[ts]
        if self.rescale_timesteps:
            new_ts = new_ts.float() * (1000.0 / self.original_num_steps)
        return self.model(x, new_ts, **kwargs)


@register_sampler(name='ddpm')
class DDPM(SpacedDiffusion):
    def p_sample(self, model, x, t):
        out = self.p_mean_variance(model, x, t)
        sample = out['mean']

        noise = torch.randn_like(x)
        if t[0] != 0:  # no noise when t == 0
            sample += torch.exp(0.5 * out['log_variance']) * noise

        return {'sample': sample, 'pred_xstart': out['pred_xstart']}


@register_sampler(name='ddim')
class DDIM(SpacedDiffusion):
    def p_sample(self, model, x, t, eta=0.0):
        out = self.p_mean_variance(model, x, t)

        eps = self.predict_eps_from_x_start(x, t, out['pred_xstart'])

        alpha_bar = extract_and_expand(self.alphas_cumprod, t, x)
        alpha_bar_prev = extract_and_expand(self.alphas_cumprod_prev, t, x)
        sigma = (
                eta
                * torch.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
                * torch.sqrt(1 - alpha_bar / alpha_bar_prev)
        )
        # Equation 12.
        noise = torch.randn_like(x)
        mean_pred = (
                out["pred_xstart"] * torch.sqrt(alpha_bar_prev)
                + torch.sqrt(1 - alpha_bar_prev - sigma ** 2) * eps
        )

        sample = mean_pred
        if t != 0:
            sample += sigma * noise

        return {"sample": sample, "pred_xstart": out["pred_xstart"]}

    def predict_eps_from_x_start(self, x_t, t, pred_xstart):
        coef1 = extract_and_expand(self.sqrt_recip_alphas_cumprod, t, x_t)
        coef2 = extract_and_expand(self.sqrt_recipm1_alphas_cumprod, t, x_t)
        return (coef1 * x_t - pred_xstart) / coef2


# =================
# Helper functions
# =================

def get_named_beta_schedule(schedule_name, num_diffusion_timesteps):
    """
    Get a pre-defined beta schedule for the given name.

    The beta schedule library consists of beta schedules which remain similar
    in the limit of num_diffusion_timesteps.
    Beta schedules may be added, but should not be removed or changed once
    they are committed to maintain backwards compatibility.
    """
    if schedule_name == "linear":
        # Linear schedule from Ho et al, extended to work for any number of
        # diffusion steps.
        scale = 1000 / num_diffusion_timesteps
        beta_start = scale * 0.0001
        beta_end = scale * 0.02
        return np.linspace(
            beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64
        )
    elif schedule_name == "cosine":
        return betas_for_alpha_bar(
            num_diffusion_timesteps,
            lambda t: math.cos((t + 0.008) / 1.008 * math.pi / 2) ** 2,
        )
    else:
        raise NotImplementedError(f"unknown beta schedule: {schedule_name}")


def betas_for_alpha_bar(num_diffusion_timesteps, alpha_bar, max_beta=0.999):
    """
    Create a beta schedule that discretizes the given alpha_t_bar function,
    which defines the cumulative product of (1-beta) over time from t = [0,1].

    :param num_diffusion_timesteps: the number of betas to produce.
    :param alpha_bar: a lambda that takes an argument t from 0 to 1 and
                      produces the cumulative product of (1-beta) up to that
                      part of the diffusion process.
    :param max_beta: the maximum beta to use; use values lower than 1 to
                     prevent singularities.
    """
    betas = []
    for i in range(num_diffusion_timesteps):
        t1 = i / num_diffusion_timesteps
        t2 = (i + 1) / num_diffusion_timesteps
        betas.append(min(1 - alpha_bar(t2) / alpha_bar(t1), max_beta))
    return np.array(betas)


# ================
# Helper function
# ================

def extract_and_expand(array, time, target):
    array = torch.from_numpy(array).to(target.device)[time].float()
    while array.ndim < target.ndim:
        array = array.unsqueeze(-1)
    return array.expand_as(target)


def expand_as(array, target):
    if isinstance(array, np.ndarray):
        array = torch.from_numpy(array)
    elif isinstance(array, np.float):
        array = torch.tensor([array])

    while array.ndim < target.ndim:
        array = array.unsqueeze(-1)

    return array.expand_as(target).to(target.device)


def _extract_into_tensor(arr, timesteps, broadcast_shape):
    """
    Extract values from a 1-D numpy array for a batch of indices.

    :param arr: the 1-D numpy array.
    :param timesteps: a tensor of indices into the array to extract.
    :param broadcast_shape: a larger shape of K dimensions with the batch
                            dimension equal to the length of timesteps.
    :return: a tensor of shape [batch_size, 1, ...] where the shape has K dims.
    """
    res = torch.from_numpy(arr).to(device=timesteps.device)[timesteps].float()
    while len(res.shape) < len(broadcast_shape):
        res = res[..., None]
    return res.expand(broadcast_shape)
