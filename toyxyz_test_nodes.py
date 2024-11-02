from PIL import Image, ImageFile
import comfy.utils
import numpy as np
import torch
import torchvision.transforms.functional as tf
import torchvision.transforms.v2 as T
import os
os.environ["OPENCV_IO_ENABLE_OPENEXR"]="1"
import time
import cv2
from pathlib import Path
from nodes import MAX_RESOLUTION, SaveImage, common_ksampler
from cv2.ximgproc import guidedFilter

import mss #Screen Capture
import win32gui
import win32ui
import win32con
import win32api
from win32gui import FindWindow, GetWindowRect #Get window size and location
import ctypes #for Find window
from ctypes import windll, wintypes

ImageFile.LOAD_TRUNCATED_IMAGES = True



def p(image):
    return image.permute([0,3,1,2])
def pb(image):
    return image.permute([0,2,3,1])

def get_surface_normal_by_depth(image: torch.Tensor, depth_m, K=None):
    """
    depth: (h, w) of float, the unit of depth is meter
    K: (3, 3) of float, the depth camera's intrinsic
    """
    K = [[1, 0], [0, 1]] if K is None else K
    fx, fy = K[0][0], K[1][1]

    #depth = image[0, :, :, :].mean(dim=-1)

    depth = image
    #depth = depth.detach().clone().numpy()
    #depth = depth_input.cpu().numpy()

    depth = np.clip(depth * 255.0, 0, 255).astype(np.float32)
    
    if len(depth.shape) == 3:
        depth = depth[:, :, 0]

    depth_safe = np.where(depth <= depth_m, np.finfo(np.float32).eps, depth)

    dz_dv, dz_du = np.gradient(depth_safe)
    du_dx = fx / depth_safe
    dv_dy = fy / depth_safe

    dz_dx = dz_du * du_dx
    dz_dy = dz_dv * dv_dy

    #normal_cross = np.dstack((-dz_dx, -dz_dy, np.ones_like(depth)))
    normal_cross = np.dstack((np.ones_like(depth), -dz_dy, -dz_dx))

    norm = np.linalg.norm(normal_cross, axis=2, keepdims=True)
    normal_unit = normal_cross / np.where(norm == 0, 1, norm)

    normal_unit[~np.isfinite(normal_unit).all(2)] = [0, 0, 1]
    
    return normal_unit

def save_image(img: torch.Tensor, path, image_format, jpg_quality, png_compress_level):
    path = str(path)

    if len(img.shape) != 3:
        raise ValueError(f"can't take image batch as input, got {img.shape[0]} images")

    img = img.permute(2, 0, 1)
    if img.shape[0] != 3:
        raise ValueError(f"image must have 3 channels, but got {img.shape[0]} channels")

    img = img.clamp(0, 1)
    img = tf.to_pil_image(img)
    
    ext = image_format

    if ext == ".jpg":
        # Save as JPEG with specified quality
        img.save(path, format="JPEG", quality=jpg_quality)
    elif ext == ".png":
        # Save as PNG with specified compression level
        img.save(path, format="PNG", compress_level=png_compress_level)
    elif ext == ".bmp":
        img.save(path, format="bmp")
    else:
        # Raise an error for unsupported file formats
        raise ValueError(f"Unsupported file format: {ext}")

    subfolder, filename = os.path.split(path)

    return {"filename": filename, "subfolder": subfolder, "type": "output"}

def find_window(name):
    
    try:
        hwnd = ctypes.windll.user32.FindWindowW(0, name)
        
        if hwnd:
            return(True)
        else:
            return(False)
    except:
        return(False)

#Get title bar thickness
def get_title_bar_thickness(hwnd):
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.pointer(rect))
    client_rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.pointer(client_rect))
    title_bar_thickness = (rect.bottom - rect.top) - (client_rect.bottom - client_rect.top)
    
    return title_bar_thickness


def capture_win_target(handle, window_capture_area_name: str, capture_full_window, window_margin):
    # Adapted from https://stackoverflow.com/questions/19695214/screenshot-of-inactive-window-printwindow-win32gui
    
    #Find target windwow and capture window
    
    window_title = win32gui.GetWindowText(handle)

    hwnd = win32gui.FindWindow(None, window_title)
    
    
    if capture_full_window == False:

        hwnd_a = win32gui.FindWindow(None, window_capture_area_name)
    
        bar_thickness = get_title_bar_thickness(hwnd_a)
    
    margin = window_margin
    
    #Get target window position and area
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
    except:
        return np.zeros((512, 512, 3), dtype=np.uint8)
        print("Wrong window handle!")

    x, y, w, h = win32gui.GetWindowRect(hwnd)
    
    left_w, top_w, right_w, bottom_w = win32gui.GetWindowRect(hwnd)
    
    
    if capture_full_window == False:
    
        left_a, top_a, right_a, bottom_a = win32gui.GetWindowRect(hwnd_a)
        
        top_a = top_a + bar_thickness - margin
        
        left_a = left_a + margin
        
        right_a = right_a - margin
        
        bottom_a = bottom_a - margin
    
    #Set crot area
    if capture_full_window == False:
        if left_a>left_w:
            x_crop = left_a-left_w
        else:
            x_crop = 0
        
        if top_a>top_w:
            y_crop = top_a-top_w
        else:
            y_crop = 0
        
        if right_a<right_w:
            x2_crop = right_a-right_w
        else:
            x2_crop = right_w
        
        if bottom_a<bottom_w:
            y2_crop = bottom_a-bottom_w
        else:
            y2_crop = bottom_w

    w = right - left
    h = bottom - top
    
    try:
        #Create bitmap
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bitmap)

        # If Special K is running, this number is 3. If not, 1
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)

        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        
    except:
        pass

    try:
        img = np.frombuffer(bmpstr, dtype=np.uint8).reshape((bmpinfo["bmHeight"], bmpinfo["bmWidth"], 4))
    except:
        return np.zeros((512, 512, 3), dtype=np.uint8)
    img = np.ascontiguousarray(img)[..., :-1]  # make image C_CONTIGUOUS and drop alpha channel
    
    img = np.array(img, dtype=np.uint8)

    #Delete hwnd
    try:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    except:
        pass
    
    #Image crop
    if capture_full_window == False:
        crop_img = img[y_crop:y2_crop, x_crop:x2_crop]
        
        crop_h,crop_w,crop_c = crop_img.shape

    if not result:  # result should be 1
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        print(f"Unable to acquire capture! Result: {result}")
        return np.zeros((512, 512, 3), dtype=np.uint8)
    
    if capture_full_window == False:
        if((crop_h>0) and (crop_w>0)):
            return crop_img
        else:
            return np.zeros((512, 512, 3), dtype=np.uint8)
       
    if capture_full_window == True:
        return img




class CaptureWebcam:

    @classmethod
    def INPUT_TYPES(cls):
        return {
                "required": {
                    "select_webcam": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 10,
                    "step": 1,
                    "display": "number"
                })
                },
            }

    RETURN_TYPES = ("IMAGE", )
    RETURN_NAMES = ("image", )
    FUNCTION = "load_image"
    
    CATEGORY = "ToyxyzTestNodes"

    def __init__(self):
        self.webcam_index = 0

    def load_image(self, select_webcam):
        capture = cv2.VideoCapture(select_webcam, cv2.CAP_DSHOW)

        try:
            # should be instantly opened
            if not capture.isOpened():
                print("Error: Could not open webcam.")

                return
            else:
                # Capture frame-by-frame
                # fake read first because the first frame is warmup and sometimes contains artifacts
                ret, frame = capture.read()
                ret, frame = capture.read()

                image = Image.fromarray(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

            if (image is None):
                print("Error: Could not read frame.")
                return

            image = np.array(image).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]

            return (image,)
        finally:
            capture.release()

    @classmethod
    def IS_CHANGED(cls):

        return
        
class LoadWebcamImage:

    @classmethod
    def INPUT_TYPES(cls):
        return {
                "required": {
                    "image_path": ("STRING", {"default": './ComfyUI/custom_nodes/ComfyUI_toyxyz_test_nodes/CaptureCam/captured_frames/capture.jpg', "multiline": False}), 
                }
            }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "load_image"
    
    CATEGORY = "ToyxyzTestNodes"

    def load_image(self, image_path):

        try:
            i = Image.open(image_path)
            i.verify()
            i = Image.open(image_path)
            
        except OSError as e:
            print("Load fail")

            try:
                time.sleep(0.05)
                i = Image.open(image_path)
                print("Try again!")
                    
            except OSError as e:
                try:
                    time.sleep(0.05)
                    i = Image.open(image_path)
                    print("Try again!")
                
                except OSError as e:
                    print("Image doesn't exist!")
                    i = Image.new(mode='RGB', size=(512, 512), color=(0, 0, 0))
   
        if not i:
            return
            
        image = i  

        image = image.convert('RGB')
        image = np.array(image).astype(np.float32) / 255.0
        image = torch.from_numpy(image)[None,]
     
        return (image, )

    @classmethod
    def IS_CHANGED(cls, image_path):
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

class SaveImagetoPath:

    INPUT_TYPES = lambda: {
        "required": {
            "path": ("STRING", {"default": "./ComfyUI/custom_nodes/ComfyUI_toyxyz_test_nodes/CaptureCam/rendered_frames/render.jpg"}),
            "image": ("IMAGE",),
            "save_sequence": (("false", "true"), {"default": "false"}),
            "image_format": ((".jpg", ".png", ".bmp"), {"default": ".jpg"}),
            "jpg_quality": ("INT", {
                    "default": 70,
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "display": "number"
                }),
            "png_compression": ("INT", {
                    "default": 5,
                    "min": 1,
                    "max": 9,
                    "step": 1,
                    "display": "number"
                }),
        },
    }
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "execute"
    
    CATEGORY = "ToyxyzTestNodes"

    def execute(
        self,
        path: str,
        image_format: str,
        image: torch.Tensor,
        save_sequence: str,
        jpg_quality,
        png_compression,
    ):
        assert isinstance(path, str)
        assert isinstance(image_format, str)
        assert isinstance(image, torch.Tensor)
        assert isinstance(save_sequence, str)

        save_sequence: bool = save_sequence == "true"

        path: Path = Path(path)

        path2 = path
        
        if save_sequence :
            count = 0
            base_filename, file_extension = path2.stem, path2.suffix
            while path2.exists():
                filename = f"{base_filename}_{format(count, '06')}{file_extension}"
                path2 = Path(path2.parent, filename)
                count += 1

        path.parent.mkdir(exist_ok=True)
        
        if image.shape[0] == 1:
            # batch has 1 image only
            save_image(
                image[0],
                path,
                image_format,
                jpg_quality,
                png_compression,
            )
            if save_sequence :
                save_image(
                    image[0],
                    path2,
                    image_format,
                    jpg_quality,
                    png_compression,
                )
            
        else:
            # batch has multiple images
            for i, img in enumerate(image):
                subpath = path.with_stem(f"{path.stem}-{i}")
                save_image(
                    img,
                    subpath,
                    image_format,
                    jpg_quality,
                    png_compression,
                )
                for i, img in enumerate(image):
                    subpath = path.with_stem(f"{path2.stem}-{i}")
                    save_image(
                        img,
                        subpath,
                        image_format,
                        jpg_quality,
                        png_compression,
                    )

        return ()
        

class LatentDelay:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "latent": ("LATENT",),
                              "delaytime": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0, "step": 0.01}),
                              }}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "LatentDelay"
    
    CATEGORY = "ToyxyzTestNodes"

    def LatentDelay(self, latent, delaytime):
        time.sleep(delaytime)
        return (latent,)

class ImageResize_Padding:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "width": ("INT", { "default": 512, "min": 0, "max": MAX_RESOLUTION, "step": 1, }),
                "height": ("INT", { "default": 512, "min": 0, "max": MAX_RESOLUTION, "step": 1, }),
                "interpolation": (["nearest", "bilinear", "bicubic", "area", "nearest-exact", "lanczos"],),
                "padding": ("BOOLEAN", { "default": True }),
                "Red":("FLOAT", { "default": 0, "min": 0, "max": 1, "step": 0.1, }),
                "Green":("FLOAT", { "default": 0, "min": 0, "max": 1, "step": 0.1, }),
                "Blue":("FLOAT", { "default": 0, "min": 0, "max": 1, "step": 0.1, }),
            }
        }

    RETURN_TYPES = ("IMAGE", "INT", "INT",)
    RETURN_NAMES = ("IMAGE", "width", "height",)
    FUNCTION = "execute"
    CATEGORY = "ToyxyzTestNodes"

    def execute(self, image, width, height, padding, Red, Green, Blue, interpolation="nearest"):
        _, oh, ow, _ = image.shape
        
        if padding is True:
            oAspectRatio = ow/oh
            tAspectRatio = width/height
            
            if oAspectRatio > tAspectRatio:
                pady = int(((ow/tAspectRatio)-oh)/2)
                padx = 0
            if oAspectRatio < tAspectRatio:
                padx = int(((oh*tAspectRatio)-ow)/2)
                pady = 0
            if oAspectRatio == tAspectRatio:
                padx = 0
                pady = 0
                
            pad = (padx, pady, padx, pady)
            image = pb(T.functional.pad(p(image), pad, fill=(Red, Green, Blue)))
            

        outputs = p(image)
        
        outputs = comfy.utils.lanczos(outputs, width, height)

        outputs = pb(outputs)

        return(outputs, outputs.shape[2], outputs.shape[1],)


class Direct_screenCap:

    @classmethod
    def IS_CHANGED(cls):

        return

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "screencap"
    CATEGORY = "ToyxyzTestNodes"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "x": ("INT", {"default": 0,"min": 0, "max": 4096, "step": 1}),
                 "y": ("INT", {"default": 0,"min": 0, "max": 4096, "step": 1}),
                 "width": ("INT", {"default": 512,"min": 0, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 512,"min": 0, "max": 4096, "step": 1}),
                 "num_frames": ("INT", {"default": 1,"min": 1, "max": 255, "step": 1}),
                 "delay": ("FLOAT", {"default": 0.1,"min": 0.0, "max": 10.0, "step": 0.01}),
                 "target_window": ("STRING", {"default": "capture"}),
                 "capture_mode": (["Default", "window", "window_crop"], ),
        },
    } 

    def screencap(self, x, y, width, height, num_frames, delay, target_window, capture_mode):
        from mss import mss
        captures = []

        
        with mss() as sct:
            
            monitor_default = {
                    "top": y,
                    "left": x,
                    "width": width,
                    "height": height
                }
            
            if capture_mode == "Default":
                monitor = monitor_default
                
            elif (capture_mode == "window" or capture_mode == "window_crop"):
            
                if target_window:
                    hwnd_a = ctypes.windll.user32.FindWindowW(0, target_window)
                    
                    margin = 11
                    
                    if hwnd_a:
                        title_bar_thickness = get_title_bar_thickness(hwnd_a)
                        
                        rect = ctypes.wintypes.RECT()
                        ctypes.windll.user32.GetWindowRect(hwnd_a, ctypes.pointer(rect))
                        
                        monitor_number = 1
                        
                        mon = sct.monitors[monitor_number]
                        
                        monitor = {
                            "top": rect.top + title_bar_thickness - margin,
                            "left": rect.left + margin,
                            "width": rect.right - (rect.left + margin) - margin,
                            "height": rect.bottom - rect.top - title_bar_thickness,
                            "mon": monitor_number,
                        }
                    else:
                        monitor = monitor_default
                        
                    if capture_mode == "window_crop":
                        monitor["top"] = monitor["top"]+y
                        monitor["left"] = monitor["left"]+x
                        monitor["width"] = width
                        monitor["height"] = height
                
                else:
                    monitor = monitor_default
                
                if (monitor["width"] <= 0 or monitor["height"] <= 0) :
                    monitor = monitor_default
            
            for _ in range(num_frames):
                sct_img = sct.grab(monitor)
                img_np = np.array(sct_img)
                img_torch = torch.from_numpy(img_np[..., [2, 1, 0]]).float() / 255.0
                captures.append(img_torch)
                
                if num_frames > 1:
                    time.sleep(delay)
        
        return (torch.stack(captures, 0),)
       

class Depth_to_normal:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "depth_min": ("FLOAT", { "default": 0, "min": -255, "max": 255, "step": 0.001, }),
                "blue_depth": ("FLOAT", { "default": 0, "min": -255, "max": 1, "step": 0.1, }),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("IMAGE",)
    FUNCTION = "execute"
    CATEGORY = "ToyxyzTestNodes"
        
    def execute(self, image: torch.Tensor, depth_min, blue_depth):
        _, oh, ow, _ = image.shape
        
        depth = image.detach().clone()
        
        if len(depth.shape) == 3:
            depth = depth[:, :, 0]
            
        K = np.array([[500, 0, 320],
                      [0, 500, 240],
                      [0, 0, 1]])    
                          
        vis_normal = lambda normal: np.uint8((normal + 1) / 2 * 255)[..., ::-1]

        
        for i in range(depth.shape[0]):
            slice = depth[i]
            
            image_np = slice.cpu().numpy()

            normal1 = get_surface_normal_by_depth(image_np, depth_min, K)
  
            normal1_blurred = vis_normal(normal1)
                
            outputs = np.array(normal1_blurred).astype(np.float32) / 255.0
            outputs[..., 1] = 1.0 - outputs[..., 1] #Flip green channel
            
            blue_channel = outputs[..., 2]  
            blue_channel = blue_depth + blue_channel * (1.0 - blue_depth) # Remap blue channel
            outputs[..., 2] = blue_channel  
            
            slice.copy_(torch.from_numpy(outputs))

        return(depth, )

class Remove_noise:
    import torch

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "guided_first": ("BOOLEAN", { "default": True }),
                "bilateral_loop": ("INT", {"default": 1, "min": 0, "max": MAX_RESOLUTION, "step": 1}),
                "d": ("INT", {"default": 15, "min": 0, "max": MAX_RESOLUTION, "step": 1}),
                "sigma_color": (
                    "INT",
                    {"default":45 , "min": 0, "max": MAX_RESOLUTION, "step": 1},
                ),
                "sigma_space": (
                    "INT",
                    {"default": 45, "min": 0, "max": MAX_RESOLUTION, "step": 1},
                ),
                "guided_loop": ("INT", {"default": 4, "min": 0, "max": MAX_RESOLUTION, "step": 1}),
                "radius": ("INT", {"default": 4, "min": 0, "max": MAX_RESOLUTION, "step": 1}),
                "eps": (
                    "INT",
                    {"default": 16, "min": 0, "max": MAX_RESOLUTION, "step": 1},
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",)

    FUNCTION = "execute"

    CATEGORY = "ToyxyzTestNodes"

    def execute(
        self,
        image: torch.Tensor,
        bilateral_loop: int,
        d: int,
        sigma_color: int,
        sigma_space: int,
        guided_loop: int,
        radius: int,
        eps: int,
        guided_first: bool,
    ):
        import numpy as np
        import cv2
        import torch
        
        diameter = d
        
        if diameter % 2 == 0:  
            diameter += 1

        def sub(image: torch.Tensor):
            guide = np.clip(255.0 * image.cpu().numpy().squeeze(), 0, 255).astype(
                np.uint8
            )
            dst = guide.copy()
            
            
            if guided_first:
            
                if guided_loop > 0:
                    for _ in range(guided_loop):
                        dst = cv2.ximgproc.guidedFilter(guide, dst, radius, eps)
                        
            if bilateral_loop > 0:
                for _ in range(bilateral_loop):
                    dst = cv2.bilateralFilter(dst, diameter, sigma_color, sigma_space)
                    
            if guided_first == False: 
            
                if guided_loop > 0:
                    for _ in range(guided_loop):
                        dst = cv2.ximgproc.guidedFilter(guide, dst, radius, eps)

            return torch.from_numpy(dst.astype(np.float32) / 255.0).unsqueeze(0)

        if len(image) > 1:
            tensors = []

            for child in image:
                tensor = sub(child)
                tensors.append(tensor)

            return (torch.cat(tensors, dim=0),)

        else:
            tensor = sub(image)
            return (tensor,)

NODE_CLASS_MAPPINGS = {
    "CaptureWebcam": CaptureWebcam,
    "LoadWebcamImage": LoadWebcamImage,
    "SaveImagetoPath": SaveImagetoPath,
    "LatentDelay": LatentDelay,
    "ImageResize_Padding": ImageResize_Padding,
    "Direct Screen Capture": Direct_screenCap,
    "Depth to normal": Depth_to_normal,
    "Remove noise": Remove_noise,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CaptureWebcam": "Capture Webcam",
    "LoadWebcamImage": "Load Webcam Image",
    "SaveImagetoPath": "Save Image to Path",
    "LatentDelay": "LatentDelay",
    "ImageResize_Padding": "ImageResize_Padding",
    "Direct_screenCap": "Direct_screenCap",
    "Depth_to_normal": "Depth_to_normal",
    "Remove_noise": "Remove_noise"
}

