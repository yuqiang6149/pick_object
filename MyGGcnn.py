from os import path
import sys

import cv2
import time
import numpy as np
import scipy.ndimage as ndimage
import pyrealsense2 as rs
import torch
from skimage.draw import circle
from skimage.feature import peak_local_max
from models.ggcnn import GGCNN
import rtde_receive
import rtde_control
import threading
from scipy.spatial.transform import Rotation as R
ip = "192.168.100.2"
rtde_r = rtde_receive.RTDEReceiveInterface(ip)
for i in range(3):
    try:
        pass
        rtde_c = rtde_control.RTDEControlInterface(ip)
        break
    except Exception:
        time.sleep(3)
        print('keep trying to connect RTDE Control')
        if i == 2:
            sys.exit()
print('rtde connect!')
model = GGCNN()
MODEL_FILE = 'ggcnn_epoch_23_cornell'
model.load_state_dict(torch.load('models/ggcnn_epoch_23_cornell_statedict.pt'))
device = torch.device("cuda:0")
model = model.cuda()
fx = 459.516
fy = 459.477
cx = 317.906
cy = 245.373

JiTing = False
def press_enter_to_JiTing():#不是完全的急停
    global JiTing
    key=input()
    JiTing=True
    key=input()
    JiTing=True
    key=input()
    JiTing=True
    key=input()
    JiTing=True
    key=input()
    JiTing=True
    sys.exit()  #exit this input thread
listener=threading.Thread(target=press_enter_to_JiTing)
listener.start()

def get_homo(R,t):
    assert R.shape == (3,3) and t.ravel().shape[0] == 3
    T = np.zeros((4,4))
    T[:3,:3] = R
    T[:3,-1] = t.ravel()
    T[-1,-1] = 1
    return T

t_RGB_DEPTH = np.array([-3.97217e-05,-0.01427,0.00537016])
R_RGB_DEPTH =np.array([[0.999997,-0.00112963,0.00210261],
                        [0.0011826,0.999678,-0.0253654],
                        [-0.00207327,0.0253678,0.999676]])
R_g_RGB = np.array([[-0.99930965,  0.03603957, -0.00902068],
                    [-0.0361714,  -0.99923445,  0.01490461],
                    [-0.00847662,  0.01522061,  0.99984823]])
t_g_RGB  = np.array([ 0.00581441, 0.16148753,-0.05332651])
T_RGB_DEPTH = get_homo(R_RGB_DEPTH,t_RGB_DEPTH)
T_g_RGB = get_homo(R_g_RGB,t_g_RGB)
T_g_c = T_g_RGB @ np.linalg.pinv(T_RGB_DEPTH)       #transformation from gripper frame to camera frame
# T_g_c = T_g_RGB
# Configure depth and color streams
pipeline = rs.pipeline()
config = rs.config()

config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
# config.enable_stream(rs.stream.color, 960, 540, rs.format.bgr8, 30)
print('222')
colorizer = rs.colorizer()
colorizer.set_option(rs.option.visual_preset,1)
colorizer.set_option(rs.option.min_distance,0)
colorizer.set_option(rs.option.max_distance,1)

pipe_profile = pipeline.start(config)

depth_sensor = pipe_profile.get_device().first_depth_sensor()
depth_sensor.set_option(rs.option.visual_preset, 5) # 5 is short range, 3 is low ambient light

print(depth_sensor.get_depth_scale())
# Execution Timing
class TimeIt:
    def __init__(self, s):
        self.s = s
        self.t0 = None
        self.t1 = None
        self.print_output = False

    def __enter__(self):
        self.t0 = time.time()

    def __exit__(self, t, value, traceback):
        self.t1 = time.time()
        if self.print_output:
            print('%s: %s' % (self.s, self.t1 - self.t0))


First_Flag = True
prev_mp = np.zeros(2)

def process_depth_image(depth, crop_size, out_size=300, return_mask=False, crop_y_offset=0):
    if depth.ndim == 3:
        imh, imw, _ = depth.shape
    else:
        imh, imw = depth.shape
    with TimeIt('1'):
        # Crop.
        depth_crop = depth[(imh - crop_size) // 2 - crop_y_offset:(imh - crop_size) // 2 + crop_size - crop_y_offset,
                           (imw - crop_size) // 2:(imw - crop_size) // 2 + crop_size]
    # depth_nan_mask = np.isnan(depth_crop).astype(np.uint8)

    # Inpaint
    # OpenCV inpainting does weird things at the border.
    with TimeIt('2'):
        depth_crop = cv2.copyMakeBorder(depth_crop, 1, 1, 1, 1, cv2.BORDER_DEFAULT)
        depth_nan_mask = np.isnan(depth_crop).astype(np.uint8)

    with TimeIt('3'):
        depth_crop[depth_nan_mask==1] = 0

    with TimeIt('4'):
        # Scale to keep as float, but has to be in bounds -1:1 to keep opencv happy.
        depth_scale = np.abs(depth_crop).max()
        depth_crop = depth_crop.astype(np.float32) / depth_scale  # Has to be float32, 64 not supported.

        with TimeIt('Inpainting'):
            depth_crop = cv2.inpaint(depth_crop, depth_nan_mask, 1, cv2.INPAINT_NS)

        # Back to original size and value range.
        depth_crop = depth_crop[1:-1, 1:-1]
        depth_crop = depth_crop * depth_scale

    with TimeIt('5'):
        # Resize
        depth_crop = cv2.resize(depth_crop, (out_size, out_size), cv2.INTER_AREA)

    if return_mask:
        with TimeIt('6'):
            depth_nan_mask = depth_nan_mask[1:-1, 1:-1]
            depth_nan_mask = cv2.resize(depth_nan_mask, (out_size, out_size), cv2.INTER_NEAREST)
        return depth_crop, depth_nan_mask
    else:
        return depth_crop

def predict(depth_raw, process_depth=True, crop_size=300, out_size=300, depth_nan_mask=None, crop_y_offset=0, filters=(2.0, 1.0, 1.0)):
    
    if process_depth:
        depth, depth_nan_mask = process_depth_image(depth_raw, crop_size, out_size=out_size, return_mask=True, crop_y_offset=crop_y_offset)

    # Inference
    depth_mean = depth.mean()
    depth = np.clip((depth - depth.mean()), -1, 1)
    depthT = torch.from_numpy(depth.reshape(1, 1, out_size, out_size).astype(np.float32)).to(device)
    with torch.no_grad():
        pred_out = model(depthT)

    points_out = pred_out[0].cpu().numpy().squeeze()
    points_out[depth_nan_mask] = 0

    # Calculate the angle map.
    cos_out = pred_out[1].cpu().numpy().squeeze()
    sin_out = pred_out[2].cpu().numpy().squeeze()
    ang_out = np.arctan2(sin_out, cos_out) / 2.0

    width_out = pred_out[3].cpu().numpy().squeeze() * 150.0  # Scaled 0-150:0-1

    # Filter the outputs.
    if filters[0]:
        points_out = ndimage.filters.gaussian_filter(points_out, filters[0])  # 3.0
    if filters[1]:
        ang_out = ndimage.filters.gaussian_filter(ang_out, filters[1])
    if filters[2]:
        width_out = ndimage.filters.gaussian_filter(width_out, filters[2])

    points_out = np.clip(points_out, 0.0, 1.0-1e-3)


    with TimeIt('Control'):
        # Calculate the best pose from the camera intrinsics.
        maxes = None
        ALWAYS_MAX = False  # Use ALWAYS_MAX = True for the open-loop solution.
        global First_Flag
        global prev_mp
        if First_Flag:  # > 0.34 initialises the max tracking when the robot is reset.
            # Track the global max.
            print('eeeeeeeeeeeeeee')
            First_Flag= False
            max_pixel = np.array(np.unravel_index(np.argmax(points_out), points_out.shape))
            #prev_mp = max_pixel.astype(np.int)
        else:
            # Calculate a set of local maxes.  Choose the one that is closes to the previous one.
            maxes = peak_local_max(points_out, min_distance=10, threshold_abs=0.1, num_peaks=2)
            if maxes.shape[0] == 0:
                return
            max_pixel = maxes[np.argmin(np.linalg.norm(maxes - prev_mp, axis=1))]

            # Keep a global copy for next iteration.
            prev_mp = (max_pixel * 0.25 + prev_mp * 0.75).astype(np.int)
        prev_mp = (max_pixel * 0.25 + prev_mp * 0.75).astype(np.int)
        ang = ang_out[max_pixel[0], max_pixel[1]]
        width = width_out[max_pixel[0], max_pixel[1]]

        # Convert max_pixel back to uncropped/resized image coordinates in order to do the camera transform.
        max_pixel = ((np.array(max_pixel) / 300.0 * crop_size) + np.array([(480 - crop_size)//2, (640 - crop_size) // 2]))
        max_pixel = np.round(max_pixel).astype(np.int)
        point_depth = depth_raw[max_pixel[0], max_pixel[1]]

        # These magic numbers are my camera intrinsic parameters.
        x = (max_pixel[1] - cx)/(fx) * point_depth
        y = (max_pixel[0] - cy)/(fy) * point_depth
        z = point_depth

        if np.isnan(z):
            print('the depth is zero. return!')
            return

    with TimeIt('Draw'):
        # Draw grasp markers on the points_out and publish it. (for visualisation)
        grasp_img = np.zeros((300, 300, 3), dtype=np.uint8)
        grasp_img[:,:,2] = (points_out * 255.0)

        grasp_img_plain = grasp_img.copy()

        rr, cc = circle(prev_mp[0], prev_mp[1], 5)
         
        for i in range(rr.shape[0]):
            if rr[i] >= 300:
                rr[i] = 299
        for i in range(cc.shape[0]):
            if cc[i] >= 300:
                cc[i] = 299
        grasp_img[rr, cc, 0] = 0
        grasp_img[rr, cc, 1] = 255
        grasp_img[rr, cc, 2] = 0
        
        depth = depth + depth_mean
        depth_center = depth[prev_mp[0],prev_mp[1]]
    return points_out, ang_out[prev_mp[0],prev_mp[1]], width_out[prev_mp[0],prev_mp[1]], grasp_img,depth_center,rr,cc,x,y,z

for i in range(5):
    joint_init = np.array([-56.15,-67.84,-120.59,-81.46,87.57,-139.09])/57.3
    joint_init=np.array([-1.51,-0.38,-2.14,-2.14,1.63,-90/57.3])

    rtde_c.moveJ(joint_init,0.2,0.1)
    while JiTing == False:

        # Wait for a coherent pair of frames: depth and color
        frames = pipeline.wait_for_frames()
        depth_frame = frames.get_depth_frame()
        depth_image = np.asanyarray(colorizer.colorize(depth_frame).get_data())
        depth = np.asanyarray(depth_frame.get_data())*0.00025

        imh, imw, _ = depth_image.shape
        crop_size = 300
        depth_crop = depth_image[(imh - crop_size) // 2:(imh - crop_size) // 2 + crop_size,
                        (imw - crop_size) // 2:(imw - crop_size) // 2 + crop_size]

        depth_raw_crop = depth[(imh - crop_size) // 2:(imh - crop_size) // 2 + crop_size,
                        (imw - crop_size) // 2:(imw - crop_size) // 2 + crop_size]                    
        # color_image = np.asanyarray(color_frame.get_data())
        
        points_out, ang_out, width_out, grasp_img, depth_center ,rr ,cc,xx,yy,zz= predict(depth,filters=(10,2,2))
        print('depth',depth_center)
        # print('z ',zz)
        # Apply colormap on depth image (image must be converted to 8-bit per pixel first)
        # depth_colormap = cv2.applyColorMap(cv2.convertScaleAbs(depth_image, alpha=0.03), cv2.COLORMAP_JET)
        # Show images


        P_c = np.array([xx,yy,zz,1]).reshape(-1,1)  #in camera frame
        end_pose = rtde_r.getActualTCPPose()
        R_b_g = R.from_rotvec(end_pose[3:]).as_matrix()

        t_b_g = np.array(end_pose[:3])
        T_b_g = get_homo(R_b_g,t_b_g)

        # print()
        P_b = T_b_g @ T_g_c @ P_c   # in base frame

        
        target_pose = np.zeros(6)
        target_pose[:3] = P_b[:-1].ravel()
        target_pose[2] += 0.2
        target_pose[3] = 3.14
        print('target',target_pose)
        cv2.namedWindow('RealSense', cv2.WINDOW_AUTOSIZE)
        cv2.imshow('RealSense', grasp_img)
        depth_crop[rr,cc,0] = 255
        depth_crop[rr,cc,1] = 0
        depth_crop[rr,cc,2] = 0

        cv2.imshow('RealSense2', depth_crop)
        cv2.waitKey(1)

    JiTing = False
    P_c = np.array([xx,yy,zz,1]).reshape(-1,1)  #in camera frame
    end_pose = rtde_r.getActualTCPPose()
    R_b_g = R.from_rotvec(end_pose[3:]).as_matrix()
    t_b_g = np.array(end_pose[:3])
    T_b_g = get_homo(R_b_g,t_b_g)


    P_b = T_b_g @ T_g_c @ P_c   # in base frame

    print(P_b)
    target_pose = np.zeros(6)
    target_pose[:3] = P_b[:-1].ravel()
    target_pose[2] += 0.17
    target_pose[3] = 3.14


    
    rtde_c.moveL(target_pose,0.05,0.05)
    time.sleep(3)



