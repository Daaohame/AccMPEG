
import torch
from torchvision import io
import os
import glob
from . import mask_utils as mu


def read_videos(video_list, logger, sort=False, normalize=True):
    '''
        Read a list of video and return two lists. 
        One is the video tensors, the other is the bandwidths.
    '''
    video_list = [{'video': read_video(video_name, logger),
                   'bandwidth': read_bandwidth(video_name),
                   'name': video_name}
                  for video_name in video_list]
    if sort:
        video_list = sorted(video_list, key=lambda x: x['bandwidth'])

    # bandwidth normalization
    gt_bandwidth = max(video['bandwidth'] for video in video_list)
    if normalize:
        for i in video_list:
            i['bandwidth'] /= gt_bandwidth

    return [i['video'] for i in video_list], [i['bandwidth'] for i in video_list], [i['name'] for i in video_list]

def read_video(video_name, logger):
    logger.info(f'Reading {video_name}')
    if 'mp4' in video_name:
        if 'compressed' not in video_name:
            return io.read_video(video_name, pts_unit='sec')[0].float().div(255).permute(0, 3, 1, 2)
        elif 'compressed' in video_name and 'qp' in video_name:
            return io.read_video(video_name, pts_unit='sec')[0].float().div(255).permute(0, 3, 1, 2)
        else:
            return mu.read_masked_video(video_name, logger)

def read_bandwidth(video_name):
    if 'compressed' not in video_name:
        return os.path.getsize(video_name)
    else:
        return sum(os.path.getsize(i) for i in glob.glob(f'{video_name}.qp[0-9]*'))
    

def write_video(video_tensor, video_name, logger):

    logger.info(f'Saving {video_name}')

    # [N, C, H, W] ==> [N, H, W, C]
    video_tensor = video_tensor.permute(0, 2, 3, 1)
    # go back to original domain
    video_tensor = video_tensor.mul(255).add_(0.5).clamp_(0, 255).to('cpu', torch.uint8)
    # lossless encode. Should be replaced
    io.write_video(video_name, video_tensor, fps=25, options={'crf': '0'})

def get_qp_from_name(video_name):

    # the video name format must be xxxxxxx_{qp}.mp4
    return int(video_name.split('.')[-2].split('_')[-1])