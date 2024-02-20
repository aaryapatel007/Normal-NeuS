import torch
import torch.nn.functional as F
import cv2 as cv
import numpy as np
from scipy.io import loadmat
import os, json
from glob import glob
from icecream import ic
from scipy.spatial.transform import Rotation as Rot
from scipy.spatial.transform import Slerp

from utils.camera_normalization import normalize_camera


# This function is borrowed from IDR: https://github.com/lioryariv/idr
def load_K_Rt_from_P(filename, P=None):
    if P is None:
        lines = open(filename).read().splitlines()
        if len(lines) == 4:
            lines = lines[1:]
        lines = [[x[0], x[1], x[2], x[3]] for x in (x.split(" ") for x in lines)]
        P = np.asarray(lines).astype(np.float32).squeeze()

    out = cv.decomposeProjectionMatrix(P)
    K = out[0]
    R = out[1]
    t = out[2]

    K = K / K[2, 2]
    intrinsics = np.eye(4)
    intrinsics[:3, :3] = K

    pose = np.eye(4, dtype=np.float32)
    pose[:3, :3] = R.transpose()
    pose[:3, 3] = (t[:3] / t[3])[:, 0]

    return intrinsics, pose

def generate_random_mask(n_images, height, width):
    # Generate random values between 0 and 1
    random_values = torch.rand(n_images, height, width, 3)

    # Threshold the values to create a binary mask
    threshold = 0.5
    mask = (random_values > threshold).float()

    return mask

class Dataset:
    def __init__(self, conf):
        super(Dataset, self).__init__()
        print('Load data: Begin')
        self.device = torch.device('cuda')
        self.conf = conf

        self.data_dir = conf.get_string('data_dir')
        self.image_dir = conf.get_string('image_dir')
        self.mask_dir = conf.get_string('mask_dir')
        self.render_cameras_name = conf.get_string('camera_params')
        self.image_num = conf.get_string('image_num')
        self.image_num = "00" + self.image_num if len(self.image_num) == 1 else "0" + self.image_num

        directories = [d for d in os.listdir(self.image_dir) if os.path.isdir(os.path.join(self.image_dir, d))]

        self.images_list = []
        # Iterate through each directory
        for directory in directories:
            # Construct the full path to the current directory
            current_directory = os.path.join(self.image_dir, directory)
            if os.path.isfile(os.path.join(current_directory, self.image_num + '.png')):
                self.images_list.append(os.path.join(current_directory, self.image_num + '.png'))

        self.n_images = len(self.images_list)
        self.images_np = np.stack([cv.imread(im_name) for im_name in self.images_list]) / 255.0
        self.masks_lis = sorted(glob(os.path.join(self.mask_dir, '*.png')))
        self.masks = generate_random_mask(self.n_images, self.images_np.shape[1], self.images_np.shape[2])

        self.intrinsics_all = []
        self.pose_all = []
        rotation_list, trans_list = [], []

        self.W2C_list = []
        self.C2W_list = []
        self.P_list = []
        # unique_camera_center_list = []
        self.camera_center_all_view = []

        camera_calib_dict = loadmat(os.path.join(self.data_dir, self.render_cameras_name))

        self.K = np.asarray(camera_calib_dict['KK'])

        self.K = self.K / self.K[2, 2]
        intrinsics = np.eye(4)
        intrinsics[:3, :3] = self.K

        for i in range(self.n_images):
            rotation = camera_calib_dict[f'Rc_{i + 1}']
            trans = camera_calib_dict[f'Tc_{i + 1}']

            # pose_W2C = np.zeros(4, dtype=np.float32)

            rotation_list.append(rotation)
            trans_list.append(trans)

            # pose_W2C[:3, :3] = rotation
            # pose_W2C[:3, 3] = trans[:, 0]

            # pose_W2C[-1, -1] = 1.0

            # self.pose_all.append(pose_W2C)

        # self.normalized_coordinate_center, self.normalized_coordinate_scale = normalize_camera(rotation_list, trans_list)

        for view_idx in range(1, 21):
            R = camera_calib_dict[f"Rc_{view_idx}"]
            t = camera_calib_dict[f"Tc_{view_idx}"]

            W2C = np.zeros((4, 4), float)
            W2C[:3, :3] = R
            W2C[:3, 3] = np.squeeze(t)
            W2C[-1, -1] = 1
            self.W2C_list.append(W2C)

            C2W = np.linalg.inv(W2C)
            self.C2W_list.append(C2W)

            # W2C_scaled = np.zeros((4, 4), float)
            # W2C_scaled[:3, :3] = self.normalized_coordinate_scale * R
            # W2C_scaled[:3, 3] = np.squeeze(t) + R @ self.normalized_coordinate_center
            # W2C_scaled[-1, -1] = 1
            # P = intrinsics[:3, :3] @ W2C_scaled[
            #                      :3]  # shape: (3, 4), project a 3D point in the world coordinate onto the pixel coordinate
            # self.P_list.append(P)

            # camera_center = - R.T @ t  # in world coordinate
            # shift then scale
            # camera_center_scaled = (camera_center - self.normalized_coordinate_center[:,
            #                                         None]) / self.normalized_coordinate_scale
            
            # unique_camera_center_list.append(camera_center_scaled)
            # unique_camera_center_list.append(camera_center)

        self.intrinsics_all = np.tile(intrinsics, (self.n_images, 1)).reshape(self.n_images, intrinsics.shape[0], intrinsics.shape[1])
        self.pose_all = np.asarray(self.C2W_list)
        self.images = torch.from_numpy(self.images_np.astype(np.float32)).to(self.device)  # [n_images, H, W, 3]
        self.H, self.W = self.images.shape[1], self.images.shape[2]

        self.intrinsics_all = torch.from_numpy(self.intrinsics_all).float().to(self.device)   # [n_images, 4, 4]
        self.intrinsics_all_inv = torch.inverse(self.intrinsics_all)  # [n_images, 4, 4]
        self.focal = self.intrinsics_all[0][0, 0]
        self.pose_all = torch.from_numpy(self.pose_all).float().to(self.device)  # [n_images, 4, 4]

        # self.unique_camera_centers = torch.from_numpy(
        #     np.squeeze(np.array(unique_camera_center_list))).float().to(self.device)  # (num_cams, 3)
        # self.projection_matrices = torch.from_numpy(np.array(self.P_list)).float().to(self.device)  # (num_cams, 3, 4)

        object_bbox_min = np.array([-1.01, -1.01, -1.01, 1.0])
        object_bbox_max = np.array([ 1.01,  1.01,  1.01, 1.0])
        # Object scale mat: region of interest to **extract mesh**
        object_scale_mat = np.eye(4)
        object_bbox_min = np.linalg.inv(object_scale_mat) @ object_scale_mat @ object_bbox_min[:, None]
        object_bbox_max = np.linalg.inv(object_scale_mat) @ object_scale_mat @ object_bbox_max[:, None]
        self.object_bbox_min = object_bbox_min[:3, 0]
        self.object_bbox_max = object_bbox_max[:3, 0]

        print('Load data: End')

    def gen_rays_at(self, img_idx, resolution_level=1):
        """
        Generate rays at world space from one camera.
        """
        l = resolution_level
        tx = torch.linspace(0, self.W - 1, self.W // l)
        ty = torch.linspace(0, self.H - 1, self.H // l)
        pixels_x, pixels_y = torch.meshgrid(tx, ty)
        p = torch.stack([pixels_x, pixels_y, torch.ones_like(pixels_y)], dim=-1) # W, H, 3
        p = torch.matmul(self.intrinsics_all_inv[img_idx, None, None, :3, :3], p[:, :, :, None]).squeeze()  # W, H, 3
        rays_v = p / torch.linalg.norm(p, ord=2, dim=-1, keepdim=True)  # W, H, 3
        rays_v = torch.matmul(self.pose_all[img_idx, None, None, :3, :3], rays_v[:, :, :, None]).squeeze()  # W, H, 3
        rays_o = self.pose_all[img_idx, None, None, :3, 3].expand(rays_v.shape)  # W, H, 3
        # rays_o = self.unique_camera_centers[img_idx, None, :3].expand(rays_v.shape) # batch_size, 3
        return rays_o.transpose(0, 1), rays_v.transpose(0, 1)

    def gen_random_rays_at(self, img_idx, batch_size):
        """
        Generate random rays at world space from one camera.
        """
        pixels_x = torch.randint(low=0, high=self.W, size=[batch_size], device = self.device)
        pixels_y = torch.randint(low=0, high=self.H, size=[batch_size], device = self.device)
        color = self.images[img_idx][(pixels_y, pixels_x)]    # batch_size, 3
        mask = self.masks[img_idx][(pixels_y, pixels_x)]      # batch_size, 3
        p = torch.stack([pixels_x, pixels_y, torch.ones_like(pixels_y)], dim=-1).float()  # batch_size, 3
        # pixel to camera coordinate transformation
        p = torch.matmul(self.intrinsics_all_inv[img_idx, None, :3, :3], p[:, :, None]).squeeze() # batch_size, 3
        rays_v = p / torch.linalg.norm(p, ord=2, dim=-1, keepdim=True)    # batch_size, 3
        rays_v = torch.matmul(self.pose_all[img_idx, None, :3, :3], rays_v[:, :, None]).squeeze()  # batch_size, 3
        rays_o = self.pose_all[img_idx, None, :3, 3].expand(rays_v.shape) # batch_size, 3
        # rays_o = self.unique_camera_centers[img_idx, None, :3].expand(rays_v.shape) # batch_size, 3
        return torch.cat([rays_o.cpu(), rays_v.cpu(), color.cpu(), mask[:, :1].cpu()], dim=-1).cuda()    # batch_size, 10

    def gen_rays_between(self, idx_0, idx_1, ratio, resolution_level=1):
        """
        Interpolate pose between two cameras.
        """
        l = resolution_level
        tx = torch.linspace(0, self.W - 1, self.W // l)
        ty = torch.linspace(0, self.H - 1, self.H // l)
        pixels_x, pixels_y = torch.meshgrid(tx, ty)
        p = torch.stack([pixels_x, pixels_y, torch.ones_like(pixels_y)], dim=-1)  # W, H, 3
        p = torch.matmul(self.intrinsics_all_inv[0, None, None, :3, :3], p[:, :, :, None]).squeeze()  # W, H, 3
        rays_v = p / torch.linalg.norm(p, ord=2, dim=-1, keepdim=True)  # W, H, 3
        trans = self.pose_all[idx_0, :3, 3] * (1.0 - ratio) + self.pose_all[idx_1, :3, 3] * ratio
        pose_0 = self.pose_all[idx_0].detach().cpu().numpy()
        pose_1 = self.pose_all[idx_1].detach().cpu().numpy()
        pose_0 = np.linalg.inv(pose_0)
        pose_1 = np.linalg.inv(pose_1)
        rot_0 = pose_0[:3, :3]
        rot_1 = pose_1[:3, :3]
        rots = Rot.from_matrix(np.stack([rot_0, rot_1]))
        key_times = [0, 1]
        slerp = Slerp(key_times, rots)
        rot = slerp(ratio)
        pose = np.diag([1.0, 1.0, 1.0, 1.0])
        pose = pose.astype(np.float32)
        pose[:3, :3] = rot.as_matrix()
        pose[:3, 3] = ((1.0 - ratio) * pose_0 + ratio * pose_1)[:3, 3]
        pose = np.linalg.inv(pose)
        rot = torch.from_numpy(pose[:3, :3]).cuda()
        trans = torch.from_numpy(pose[:3, 3]).cuda()
        rays_v = torch.matmul(rot[None, None, :3, :3], rays_v[:, :, :, None]).squeeze()  # W, H, 3
        rays_o = trans[None, None, :3].expand(rays_v.shape)  # W, H, 3
        return rays_o.transpose(0, 1), rays_v.transpose(0, 1)

    def near_far_from_sphere(self, rays_o, rays_d):
        a = torch.sum(rays_d**2, dim=-1, keepdim=True)
        b = 2.0 * torch.sum(rays_o * rays_d, dim=-1, keepdim=True)
        mid = 0.5 * (-b) / a
        near = mid - 1.0
        far = mid + 1.0
        return near, far

    def image_at(self, idx, resolution_level):
        img = cv.imread(self.images_list[idx])
        return (cv.resize(img, (self.W // resolution_level, self.H // resolution_level))).clip(0, 255)

