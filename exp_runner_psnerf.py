import os
import logging
import argparse
import numpy as np
import cv2 as cv
import trimesh
import logging
import torch
import matplotlib.pyplot as plt
import seaborn as sns
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from shutil import copyfile
from tqdm import tqdm
from pyhocon import ConfigFactory
from models.dataset_psnerf import Dataset
from models.fields import RenderingNetwork, SDFNetwork, SingleVarianceNetwork, NeRF
from models.renderer_psnerf import NeuSRenderer
from utils.metrics import MAE, PSNR, SSIM, LPIPS

bg = lambda x, mask: x*mask + (mask ^ 1)  ## white background
to_numpy = lambda x: x.detach().cpu().numpy()

class Runner:
    def __init__(self, conf_path, mode='train', case='CASE_NAME', is_continue=False, checkpoint = False):
        self.device = torch.device('cuda')

        # Configuration
        self.conf_path = conf_path
        f = open(self.conf_path)
        conf_text = f.read()
        conf_text = conf_text.replace('CASE_NAME', case)
        f.close()

        self.conf = ConfigFactory.parse_string(conf_text)
        self.conf['dataset.data_dir'] = self.conf['dataset.data_dir'].replace('CASE_NAME', case)
        self.base_exp_dir = self.conf['general.base_exp_dir']
        os.makedirs(self.base_exp_dir, exist_ok=True)
        self.dataset = Dataset(self.conf['dataset'])
        self.iter_step = 0

        # Training parameters
        self.end_iter = self.conf.get_int('train.end_iter')
        self.save_freq = self.conf.get_int('train.save_freq')
        self.report_freq = self.conf.get_int('train.report_freq')
        self.val_freq = self.conf.get_int('train.val_freq')
        self.val_mesh_freq = self.conf.get_int('train.val_mesh_freq')
        self.batch_size = self.conf.get_int('train.batch_size')
        self.validate_resolution_level = self.conf.get_int('train.validate_resolution_level')
        self.learning_rate = self.conf.get_float('train.learning_rate')
        self.learning_rate_alpha = self.conf.get_float('train.learning_rate_alpha')
        self.use_white_bkgd = self.conf.get_bool('train.use_white_bkgd')
        self.warm_up_end = self.conf.get_float('train.warm_up_end', default=0.0)
        self.anneal_end = self.conf.get_float('train.anneal_end', default=0.0)

        # Weights
        self.igr_weight = self.conf.get_float('train.igr_weight')
        self.mask_weight = self.conf.get_float('train.mask_weight')
        self.normal_weight = self.conf.get_float('train.normal_weight')
        self.depth_weight = self.conf.get_float('train.depth_weight')
        self.sparse_weight = self.conf.get_float('train.sparse_weight')
        self.bias_weight = self.conf.get_float('train.bias_weight')
        self.is_continue = is_continue
        self.mode = mode
        self.model_list = []
        self.writer = None

        # Networks
        params_to_train = []
        self.nerf_outside = NeRF(**self.conf['model.nerf']).to(self.device)
        self.sdf_network = SDFNetwork(**self.conf['model.sdf_network']).to(self.device)
        self.deviation_network = SingleVarianceNetwork(**self.conf['model.variance_network']).to(self.device)
        self.color_network = RenderingNetwork(**self.conf['model.rendering_network']).to(self.device)
        params_to_train += list(self.nerf_outside.parameters())
        params_to_train += list(self.sdf_network.parameters())
        params_to_train += list(self.deviation_network.parameters())
        params_to_train += list(self.color_network.parameters())
        self.optimizer = torch.optim.Adam(params_to_train, lr=self.learning_rate)

        self.renderer = NeuSRenderer(self.nerf_outside,
                                     self.sdf_network,
                                     self.deviation_network,
                                     self.color_network,
                                     **self.conf['model.neus_renderer'])

        # Load checkpoint
        latest_model_name = None
        if checkpoint:
             model_list_raw = os.listdir(os.path.join(self.base_exp_dir, 'checkpoints'))
             model_list = []
             for model_name in model_list_raw:
                 if model_name[-3:] == 'pth' and int(model_name[5:-4]) <= self.end_iter:
                     model_list.append(model_name)
             model_list.sort()
             latest_model_name = model_list[-1]

        if is_continue:
            model_list_raw = os.listdir(os.path.join(self.base_exp_dir, 'checkpoints'))
            model_list = []
            for model_name in model_list_raw:
                if model_name[-3:] == 'pth' and int(model_name[5:-4]) <= self.end_iter:
                    model_list.append(model_name)
            model_list.sort()
            latest_model_name = model_list[-1]

        if latest_model_name is not None:
            logging.info('Find checkpoint: {}'.format(latest_model_name))
            self.load_checkpoint(latest_model_name)

        # Backup codes and configs for debug
        # if self.mode[:5] == 'train':
        #     self.file_backup()

    def train(self):
        self.writer = SummaryWriter(log_dir=os.path.join(self.base_exp_dir, 'logs'))
        self.update_learning_rate()
        res_step = self.end_iter - self.iter_step
        image_perm = self.get_image_perm()
        
        self.best_val_metric = None
        self.best_val_iter = 0
        self.patience = self.conf.get_int('train.patience', default=10)  # Set a default patience value, e.g., 10
        self.early_stop = False
        self.losses_val, self.psnrs_val = [], []
        all_normal_mae = []

        for _ in tqdm(range(res_step)):
            if self.early_stop:
                break
            data, pose_c2w, intrinsics = self.dataset.gen_random_rays_at_psnerf(image_perm[self.iter_step % len(image_perm)], self.batch_size)

            # rays_o, rays_d, true_rgb, mask = data[:, :3], data[:, 3: 6], data[:, 6: 9], data[:, 9: 10]
            rays_o, rays_d, true_rgb, true_normal, mask, normal_mask = data[:, :3], data[:, 3: 6], data[:, 6: 9], data[:, 9: 12], data[:, 12: 13], data[:, 13: 14]
            near, far = self.dataset.near_far_from_sphere(rays_o, rays_d)

            background_rgb = None
            if self.use_white_bkgd:
                background_rgb = torch.ones([1, 3])

            if self.mask_weight > 0.0:
                mask = (mask > 0.5).int()
            else:
                mask = torch.ones(true_rgb.shape[0], 1).to(self.device)

            mask_sum = mask.sum() + 1e-5
            render_out = self.renderer.render(rays_o, rays_d, near, far,
                                              background_rgb=background_rgb,
                                              cos_anneal_ratio=self.get_cos_anneal_ratio())

            color_fine = render_out['color_fine']
            s_val = render_out['s_val']
            cdf_fine = render_out['cdf_fine']
            gradient_error = render_out['gradient_error']
            sparse_loss = render_out['sparse_loss']
            bias_loss = render_out['bias_loss']
            weight_max = render_out['weight_max']
            weight_sum = render_out['weight_sum']
            surface_points_normal = render_out['surface_points_gradients']

            # Photometric Loss
            color_error = (color_fine - true_rgb) * mask
            color_fine_loss = F.l1_loss(color_error, torch.zeros_like(color_error), reduction='sum') / mask_sum
            psnr = 20.0 * torch.log10(1.0 / (((color_fine - true_rgb)**2 * mask).sum() / (mask_sum * 3.0)).sqrt())
            # ssim = SSIM(bg(to_numpy(color_fine), to_numpy(mask)), bg(to_numpy(true_rgb), to_numpy(mask)), to_numpy(mask))
            # lpips = LPIPS()(bg(color_fine, mask), bg(true_rgb, mask), mask)
            eikonal_loss = gradient_error

            mask_loss = F.binary_cross_entropy(weight_sum.clip(1e-3, 1.0 - 1e-3), mask.float())

            # true_normal = torch.einsum('bij,bnj->bni', self.dataset.pose_all[image_perm[self.iter_step % len(image_perm)],:3,:3] * torch.tensor([[[1,-1,-1]]],dtype=torch.float32).to(self.device), true_normal[network_mask].unsqueeze(0)).squeeze(0)
            # true_normal = true_normal / torch.norm(true_normal, dim = -1, keepdim = True)
            
            # Normal Loss
            normal_error = (surface_points_normal - true_normal) * normal_mask

            # # calculate MAE for normal
            normal_mae = MAE(bg(to_numpy(surface_points_normal), to_numpy(normal_mask.int())), bg(to_numpy(true_normal), to_numpy(normal_mask.int())), to_numpy(normal_mask.int()))
            all_normal_mae.append(normal_mae[0])
            
            if(normal_error.shape[0] > 0):
                normal_loss = F.l1_loss(normal_error, torch.zeros_like(normal_error))
            else:
                normal_loss = 0.0

            loss = color_fine_loss +\
                   eikonal_loss * self.igr_weight +\
                   mask_loss * self.mask_weight +\
                    normal_loss * self.normal_weight +\
                        sparse_loss * self.sparse_weight +\
                            bias_loss * self.bias_weight

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            self.iter_step += 1

            self.writer.add_scalar('Loss/loss', loss, self.iter_step)
            self.writer.add_scalar('Loss/color_loss', color_fine_loss, self.iter_step)
            self.writer.add_scalar('Loss/eikonal_loss', eikonal_loss, self.iter_step)
            self.writer.add_scalar('Loss/sparse_loss', sparse_loss, self.iter_step)
            self.writer.add_scalar('Loss/bias_loss', bias_loss, self.iter_step)
            self.writer.add_scalar('Loss/normal_loss', normal_loss, self.iter_step)
            self.writer.add_scalar('Loss/mask_loss', mask_loss, self.iter_step)
            # self.writer.add_scalar('Loss/depth_loss', depth_loss, self.iter_step)
            self.writer.add_scalar('Statistics/s_val', s_val.mean(), self.iter_step)
            self.writer.add_scalar('Statistics/cdf', (cdf_fine[:, :1] * mask).sum() / mask_sum, self.iter_step)
            self.writer.add_scalar('Statistics/weight_max', (weight_max * mask).sum() / mask_sum, self.iter_step)
            self.writer.add_scalar('Statistics/psnr', psnr, self.iter_step)
            # self.writer.add_scalar('Statistics/ssim', ssim, self.iter_step)
            # self.writer.add_scalar('Statistics/lpips', lpips, self.iter_step)
            self.writer.add_scalar('Statistics/normal_mae', normal_mae[0], self.iter_step)

            if self.iter_step % self.report_freq == 0:
                print(self.base_exp_dir)
                print('iter:{:8>d} loss = {} normal_loss = {} sparse_loss = {} bias_loss = {} eikonal_loss = {} mask_loss = {} lr = {} PSNR = {} normal MAE = {}'.format(self.iter_step, loss, normal_loss, sparse_loss, bias_loss, eikonal_loss, mask_loss, self.optimizer.param_groups[0]['lr'], psnr, normal_mae[0]))
                # print('iter:{:8>d} loss = {} eikonal_loss = {} mask_loss = {} lr = {} PSNR = {}'.format(self.iter_step, loss, eikonal_loss, mask_loss, self.optimizer.param_groups[0]['lr'], psnr))

            if self.iter_step % self.save_freq == 0:
                self.save_checkpoint()

            if self.iter_step % self.val_freq == 0:
                self.validate_image()
                self.losses_val.append(loss.item())
                self.psnrs_val.append(psnr.item())
                if self.best_val_metric is None or loss < self.best_val_metric:
                    self.best_val_metric = loss
                    self.best_val_iter = self.iter_step
                    patience_counter = 0
                else:
                    patience_counter += 1
                    if patience_counter >= self.patience:
                        self.early_stop = True
                        print(f"Early stopping at iteration {self.iter_step} (best val PSNR: {self.best_val_metric:.2f} at iteration {self.best_val_iter})")
                        break

            if self.iter_step == self.end_iter:
                self.validate_mesh(world_space=False, resolution=512)
            elif self.iter_step % self.val_mesh_freq == 0:
                if(self.iter_step > 24000):
                    self.validate_mesh(save_numpy_sdf = True)
                else:
                    self.validate_mesh()

            self.update_learning_rate()

            if self.iter_step % len(image_perm) == 0:
                image_perm = self.get_image_perm()

        logging.getLogger('matplotlib.font_manager').disabled = True

        # sns.set_style("darkgrid")

        # Plot and save the loss curve
        plt.figure(figsize=(10, 6))
        losses = self.losses_val
        iterations = list(range(len(losses)))
        plt.plot(iterations, losses, '-', label='Loss', linewidth=2)
        plt.xlabel('Iteration', fontsize=14)
        plt.ylabel('Loss', fontsize=14)
        plt.title('Loss Curve', fontsize=16)
        plt.legend(fontsize=12)
        plt.grid(True, linestyle='--')
        plt.savefig(os.path.join(self.base_exp_dir, 'loss_curve.png'), dpi=600, bbox_inches='tight')

        # Plot and save the PSNR curve
        plt.figure(figsize=(10, 6))
        psnrs = self.psnrs_val
        iterations = list(range(len(psnrs)))
        plt.plot(iterations, psnrs, '-', label='PSNR', linewidth=2)
        plt.xlabel('Iteration', fontsize=14)
        plt.ylabel('PSNR', fontsize=14)
        plt.title('PSNR Curve', fontsize=16)
        plt.legend(fontsize=12)
        plt.grid(True, linestyle='--')
        plt.savefig(os.path.join(self.base_exp_dir, 'psnr_curve.png'), dpi=600, bbox_inches='tight')
        
        self.validate_mesh(world_space=False, resolution=512)

        # mean of all normal mae
        print('Mean of all normal mae: ', np.mean(all_normal_mae))
        # Close the SummaryWriter
        self.writer.close()

    def get_image_perm(self):
        return torch.randperm(self.dataset.n_images, device = self.device)

    def get_cos_anneal_ratio(self):
        if self.anneal_end == 0.0:
            return 1.0
        else:
            return np.min([1.0, self.iter_step / self.anneal_end])

    def update_learning_rate(self):
        if self.iter_step < self.warm_up_end:
            learning_factor = self.iter_step / self.warm_up_end
        else:
            alpha = self.learning_rate_alpha
            progress = (self.iter_step - self.warm_up_end) / (self.end_iter - self.warm_up_end)
            learning_factor = (np.cos(np.pi * progress) + 1.0) * 0.5 * (1 - alpha) + alpha

        for g in self.optimizer.param_groups:
            g['lr'] = self.learning_rate * learning_factor

    def file_backup(self):
        dir_lis = self.conf['general.recording']
        os.makedirs(os.path.join(self.base_exp_dir, 'recording'), exist_ok=True)
        for dir_name in dir_lis:
            cur_dir = os.path.join(self.base_exp_dir, 'recording', dir_name)
            os.makedirs(cur_dir, exist_ok=True)
            files = os.listdir(dir_name)
            for f_name in files:
                if f_name[-3:] == '.py':
                    copyfile(os.path.join(dir_name, f_name), os.path.join(cur_dir, f_name))

        copyfile(self.conf_path, os.path.join(self.base_exp_dir, 'recording', 'config.conf'))

    def load_checkpoint(self, checkpoint_name):
        checkpoint = torch.load(os.path.join(self.base_exp_dir, 'checkpoints', checkpoint_name), map_location=self.device)
        self.nerf_outside.load_state_dict(checkpoint['nerf'])
        self.sdf_network.load_state_dict(checkpoint['sdf_network_fine'])
        self.deviation_network.load_state_dict(checkpoint['variance_network_fine'])
        self.color_network.load_state_dict(checkpoint['color_network_fine'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.iter_step = checkpoint['iter_step']

        logging.info('End')

    def save_checkpoint(self):
        checkpoint = {
            'nerf': self.nerf_outside.state_dict(),
            'sdf_network_fine': self.sdf_network.state_dict(),
            'variance_network_fine': self.deviation_network.state_dict(),
            'color_network_fine': self.color_network.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'iter_step': self.iter_step,
        }

        os.makedirs(os.path.join(self.base_exp_dir, 'checkpoints'), exist_ok=True)
        torch.save(checkpoint, os.path.join(self.base_exp_dir, 'checkpoints', 'ckpt_{:0>6d}.pth'.format(self.iter_step)))
        
    def evaluate(self):
        # self.validate_image()
        image_perm = self.get_image_perm()
    
        all_normal_mae, all_psnr, all_ssim, all_lpips = [], [], [], []

        for i in tqdm(range(image_perm.shape[0])):
            data, _, _ = self.dataset.gen_random_rays_at_psnerf(image_perm[i], self.batch_size)

            # rays_o, rays_d, true_rgb, mask = data[:, :3], data[:, 3: 6], data[:, 6: 9], data[:, 9: 10]
            rays_o, rays_d, true_rgb, true_normal, mask, normal_mask = data[:, :3], data[:, 3: 6], data[:, 6: 9], data[:, 9: 12], data[:, 12: 13], data[:, 13: 14]
            near, far = self.dataset.near_far_from_sphere(rays_o, rays_d)

            background_rgb = None
            if self.use_white_bkgd:
                background_rgb = torch.ones([1, 3])

            if self.mask_weight > 0.0:
                mask = (mask > 0.5).int()
            else:
                mask = torch.ones(true_rgb.shape[0], 1).to(self.device)

            mask_sum = mask.sum() + 1e-5
            render_out = self.renderer.render(rays_o, rays_d, near, far,
                                              background_rgb=background_rgb,
                                              cos_anneal_ratio=self.get_cos_anneal_ratio())

            color_fine = render_out['color_fine']
            surface_points_normal = render_out['surface_points_gradients']

            # Photometric Loss
            psnr = 20.0 * torch.log10(1.0 / (((color_fine - true_rgb)**2 * mask).sum() / (mask_sum * 3.0)).sqrt())
            # ssim = SSIM(bg(to_numpy(color_fine), to_numpy(mask)), bg(to_numpy(true_rgb), to_numpy(mask)), to_numpy(mask))
            # lpips = LPIPS()(bg(color_fine, mask), bg(true_rgb, mask), mask)
            
            all_psnr.append(psnr.item())
            # all_ssim.append(ssim)
            # all_lpips.append(lpips)

            # # calculate MAE for normal
            normal_mae = MAE(bg(to_numpy(surface_points_normal), to_numpy(normal_mask.int())), bg(to_numpy(true_normal), to_numpy(normal_mask.int())), to_numpy(normal_mask.int()))
            all_normal_mae.append(normal_mae[0])
            
        # mean of all normal mae
        print('Mean of all normal mae: ', np.mean(all_normal_mae))
        print('Mean of all PSNR: ', np.mean(all_psnr))
        print('Mean of all SSIM: ', np.mean(all_ssim))
        print('Mean of all LPIPS: ', np.mean(all_lpips))

    def validate_image(self, idx=-1, resolution_level=-1):
        if idx < 0:
            idx = np.random.randint(self.dataset.n_images)

        print('Validate: iter: {}, camera: {}'.format(self.iter_step, idx))

        if resolution_level < 0:
            resolution_level = self.validate_resolution_level

        l = resolution_level
        tx = torch.linspace(0, self.dataset.W, (self.dataset.W // l) + 1, dtype = torch.long, device = self.device)[:-1]
        ty = torch.linspace(0, self.dataset.H, (self.dataset.H // l) + 1, dtype = torch.long, device = self.device)[:-1]
        pixels_x, pixels_y = torch.meshgrid(tx, ty)
        # p = torch.stack([pixels_x, pixels_y, torch.ones_like(pixels_y)], dim=-1) # W, H, 3
        color = self.dataset.images[idx][(pixels_y, pixels_x)]    # batch_size, 3
        mask = self.dataset.masks[idx][(pixels_y, pixels_x)]      # batch_size, 1
        normal = self.dataset.normals[idx][(pixels_y, pixels_x)]  # batch_size, 3
        normal_mask = self.dataset.normal_masks[idx][(pixels_y, pixels_x)]  # batch_size, 1

        rgb_color_gt = color.reshape(-1, 3).detach().cpu().numpy()
        mask_gt = mask.reshape(-1, 3)[:, :1].int().detach().cpu().numpy()
        normal_gt = normal.reshape(-1, 3).detach().cpu().numpy()
        normal_mask_gt = normal_mask.reshape(-1, 3)[:, :1].int().detach().cpu().numpy()

        rays_o, rays_d, pose_c2w, intrinsics = self.dataset.gen_rays_at_psnerf(idx, resolution_level=resolution_level)
        H, W, _ = rays_o.shape
        rays_o = rays_o.reshape(-1, 3).split(self.batch_size)
        rays_d = rays_d.reshape(-1, 3).split(self.batch_size)

        out_rgb_fine = []
        out_normal_fine = []
        out_depth_fine = []

        for rays_o_batch, rays_d_batch in zip(rays_o, rays_d):
            near, far = self.dataset.near_far_from_sphere(rays_o_batch, rays_d_batch)
            background_rgb = torch.ones([1, 3]) if self.use_white_bkgd else None

            render_out = self.renderer.render(rays_o_batch,
                                              rays_d_batch,
                                              near,
                                              far,
                                              cos_anneal_ratio=self.get_cos_anneal_ratio(),
                                              background_rgb=background_rgb)

            def feasible(key): return (key in render_out) and (render_out[key] is not None)

            if feasible('color_fine'):
                out_rgb_fine.append(render_out['color_fine'].detach().cpu().numpy())
            if feasible('gradients') and feasible('weights'):
                n_samples = self.renderer.n_samples + self.renderer.n_importance
                normals = render_out['gradients'] * render_out['weights'][:, :n_samples, None]
                if feasible('inside_sphere'):
                    normals = normals * render_out['inside_sphere'][..., None]
                normals = normals.sum(dim=1).detach().cpu().numpy()
                out_normal_fine.append(normals)
            if feasible('surface_points') and feasible('mid_inside_sphere'):
                surface_points = render_out['surface_points']
                mid_inside_sphere = render_out['mid_inside_sphere']

                surface_points_camera = torch.matmul(pose_c2w[:3, :3].permute(1, 0), surface_points.permute(0, 2, 1))
                trans = - torch.matmul(pose_c2w[:3, :3].permute(1, 0), pose_c2w[:3, 3, None])
                surface_points_camera = surface_points_camera + trans

                surface_points_pixels = torch.matmul(intrinsics[:3, :3], surface_points_camera)
                estim_depth = surface_points_pixels[:, 2, :] * mid_inside_sphere
                out_depth_fine.append(estim_depth.detach().cpu().numpy())

            del render_out

        img_fine = None
        rgb_color_pred = np.concatenate(out_rgb_fine, axis=0)
        if len(out_rgb_fine) > 0:
            img_fine = (rgb_color_pred.reshape([H, W, 3, -1]) * 256).clip(0, 255)

        ssim = SSIM(bg(rgb_color_gt, mask_gt), bg(rgb_color_pred, mask_gt), mask_gt)
        psnr = 20.0 * np.log10(1.0 / np.sqrt(((rgb_color_pred - rgb_color_gt)**2 * mask_gt).sum() / (mask_gt.sum() * 3.0)))

        normal_img = None
        if len(out_normal_fine) > 0:
            normal_img = np.concatenate(out_normal_fine, axis=0)
            normal_mae = MAE(bg(normal_gt, normal_mask_gt), bg(normal_img, normal_mask_gt), normal_mask_gt)
            rot = (self.dataset.pose_all[idx, :3, :3].detach().cpu().numpy()).T
            normal_img = np.matmul(rot[None, :, :], normal_img[:, :, None]).reshape([H, W, 3, -1])
            normal_img = normal_img / np.linalg.norm(normal_img, axis=2, keepdims=True)
            normal_img = (normal_img + 1.0) * 0.5 * 255.0
        
        depth_fine = None
        if len(out_depth_fine) > 0:
            depth_fine = np.concatenate(out_depth_fine, axis=0).reshape([H, W, 1, -1])
            depth_fine[depth_fine < 0] = 0

        os.makedirs(os.path.join(self.base_exp_dir, 'validations_fine'), exist_ok=True)
        os.makedirs(os.path.join(self.base_exp_dir, 'normals'), exist_ok=True)
        os.makedirs(os.path.join(self.base_exp_dir, 'depths_estim'), exist_ok=True)

        for i in range(img_fine.shape[-1]):
            if len(out_rgb_fine) > 0:
                cv.imwrite(os.path.join(self.base_exp_dir,
                                        'validations_fine',
                                        '{:0>8d}_{}_{}.png'.format(self.iter_step, i, idx)),
                           np.concatenate([img_fine[..., i],
                                           self.dataset.image_at(idx, resolution_level=resolution_level)]))
            if len(out_normal_fine) > 0:
                cv.imwrite(os.path.join(self.base_exp_dir,
                                        'normals',
                                        '{:0>8d}_{}_{}.png'.format(self.iter_step, i, idx)),
                           normal_img[..., i])
            
            if len(out_depth_fine) > 0:
                cv.imwrite(os.path.join(self.base_exp_dir,
                                        'depths_estim',
                                        '{:0>8d}_{}_{}.png'.format(self.iter_step, i, idx)),
                           (depth_fine[..., i] / depth_fine[..., i].max() * 255).astype(np.uint8))

    def render_novel_image(self, idx_0, idx_1, ratio, resolution_level):
        """
        Interpolate view between two cameras.
        """
        rays_o, rays_d = self.dataset.gen_rays_between(idx_0, idx_1, ratio, resolution_level=resolution_level)
        H, W, _ = rays_o.shape
        rays_o = rays_o.reshape(-1, 3).split(self.batch_size)
        rays_d = rays_d.reshape(-1, 3).split(self.batch_size)

        out_rgb_fine = []
        for rays_o_batch, rays_d_batch in zip(rays_o, rays_d):
            near, far = self.dataset.near_far_from_sphere(rays_o_batch, rays_d_batch)
            background_rgb = torch.ones([1, 3]) if self.use_white_bkgd else None

            render_out = self.renderer.render(rays_o_batch,
                                              rays_d_batch,
                                              near,
                                              far,
                                              cos_anneal_ratio=self.get_cos_anneal_ratio(),
                                              background_rgb=background_rgb)

            out_rgb_fine.append(render_out['color_fine'].detach().cpu().numpy())

            del render_out

        img_fine = (np.concatenate(out_rgb_fine, axis=0).reshape([H, W, 3]) * 256).clip(0, 255).astype(np.uint8)
        return img_fine

    def validate_mesh(self, world_space=False, resolution=64, threshold=0.0, save_numpy_sdf = False):
        bound_min = torch.tensor(self.dataset.object_bbox_min, dtype=torch.float32)
        bound_max = torch.tensor(self.dataset.object_bbox_max, dtype=torch.float32)

        vertices, triangles =\
            self.renderer.extract_geometry(bound_min, bound_max, resolution=resolution, threshold=threshold, save_numpy_sdf = save_numpy_sdf, case = args.case)
        os.makedirs(os.path.join(self.base_exp_dir, 'meshes'), exist_ok=True)

        if world_space:
            vertices = vertices * self.dataset.scale_mats_np[0][0, 0] + self.dataset.scale_mats_np[0][:3, 3][None]

        mesh = trimesh.Trimesh(vertices, triangles)
        mesh.export(os.path.join(self.base_exp_dir, 'meshes', '{:0>8d}.ply'.format(self.iter_step)))

        logging.info('End')

    def interpolate_view(self, img_idx_0, img_idx_1):
        images = []
        n_frames = 60
        for i in range(n_frames):
            print(i)
            images.append(self.render_novel_image(img_idx_0,
                                                  img_idx_1,
                                                  np.sin(((i / n_frames) - 0.5) * np.pi) * 0.5 + 0.5,
                          resolution_level=4))
        for i in range(n_frames):
            images.append(images[n_frames - i - 1])

        fourcc = cv.VideoWriter_fourcc(*'mp4v')
        video_dir = os.path.join(self.base_exp_dir, 'render')
        os.makedirs(video_dir, exist_ok=True)
        h, w, _ = images[0].shape
        writer = cv.VideoWriter(os.path.join(video_dir,
                                             '{:0>8d}_{}_{}.mp4'.format(self.iter_step, img_idx_0, img_idx_1)),
                                fourcc, 30, (w, h))

        for image in images:
            writer.write(image)

        writer.release()


if __name__ == '__main__':
    print('Hello Wooden')

    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    FORMAT = "[%(filename)s:%(lineno)s - %(funcName)20s() ] %(message)s"
    logging.basicConfig(level=logging.DEBUG, format=FORMAT)

    parser = argparse.ArgumentParser()
    parser.add_argument('--conf', type=str, default='./confs/base.conf')
    parser.add_argument('--mode', type=str, default='train')
    parser.add_argument('--mcube_threshold', type=float, default=0.0)
    parser.add_argument('--is_continue', default=False, action="store_true")
    parser.add_argument('--checkpoint', type=int, default=0)
    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--case', type=str, default='')

    args = parser.parse_args()

    torch.cuda.set_device(args.gpu)
    runner = Runner(args.conf, args.mode, args.case, args.is_continue)

    if args.mode == 'train':
        runner.train()
    elif args.mode == 'evaluate':
        runner.evaluate()
    elif args.mode == 'validate_mesh':
        runner.validate_mesh(world_space=False, resolution=512, threshold=args.mcube_threshold, save_numpy_sdf = False)
    elif args.mode.startswith('interpolate'):  # Interpolate views given two image indices
        _, img_idx_0, img_idx_1 = args.mode.split('_')
        img_idx_0 = int(img_idx_0)
        img_idx_1 = int(img_idx_1)
        runner.interpolate_view(img_idx_0, img_idx_1)