
import os
from itertools import product
import yaml


# v_list = ['dashcam_%d_test' % (i+1) for i in range(4)] + ['trafficcam_%d_test' % (i+1) for i in range(4)]
# v_list = [v_list[0]]

v_list = ['visdrone/videos/vis_171']
# v_list = [v_list[2]]
base = 34
tile = 16
perc = 20
model = 'COCO'

for v in v_list:

    # output = f'{v}_compressed_ground_truth_2%_tile_16.mp4'
    output = f'{v}_compressed_blackgen.hevc'

    os.system(f'python compress_blackgen.py -i {v}_qp_{base}.hevc '
              f' {v}_qp_22.hevc -s {v}.yuv -o {output} --tile_size {tile}  -p maskgen_pths/fcn_mask_{model}.pth'
              f' --tile_percentage {perc}')
    os.system(f'python inference.py -i {output}')
    os.system(f'python examine.py -i {output} -g {v}_qp_22.hevc')
