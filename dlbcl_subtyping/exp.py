import os
import warnings

from glob import glob
from tqdm import tqdm
from pydantic import Field
from PIL import Image, ImageDraw
import cv2
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt, colors as mcolors
from matplotlib.colors import LinearSegmentedColormap, Normalize
import seahorse as sns
import h5py
import umap
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
import hdbscan
import torch
import timm

from .utils import BaseMLCLI, BaseMLArgs

warnings.filterwarnings('ignore', category=FutureWarning, message='.*force_all_finite.*')



# def is_white_patch(patch, white_threshold=200, white_ratio=0.7):
#     gray_patch = np.mean(patch, axis=-1)
#     white_pixels = np.sum(gray_patch > white_threshold)
#     total_pixels = patch.shape[0] * patch.shape[1]
#     return (white_pixels / total_pixels) > white_ratio


def is_white_patch_std_sat(patch, rgb_std_threshold=5.0, sat_threshold=10, white_ratio=0.7, verbose=False):
    # white: RGB std < 5.0 and Sat(HSV) < 15
    rgb_std_pixels = np.std(patch, axis=2) < rgb_std_threshold
    patch_hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    sat_pixels = patch_hsv[:, :, 1] < sat_threshold
    white_pixels = np.sum(rgb_std_pixels | sat_pixels)
    total_pixels = patch.shape[0] * patch.shape[1]
    white_ratio_calculated = white_pixels / total_pixels
    if verbose:
        print('whi' if white_ratio_calculated > white_ratio else 'use',
              'and{:.3f} or{:.3f} std{:.3f} sat{:.4f}'.format(
                    np.sum(rgb_std_pixels & sat_pixels)/total_pixels,
                    np.sum(rgb_std_pixels | sat_pixels)/total_pixels,
                    np.sum(rgb_std_pixels)/total_pixels,
                    np.sum(sat_pixels)/total_pixels
                ),
            )
    return white_ratio_calculated > white_ratio

class CLI(BaseMLCLI):
    class CommonArgs(BaseMLArgs):
        # This includes `--seed` param
        device: str = 'cuda'
        pass

    class ClusterArgs(CommonArgs):
        target: str = Field('cluster', s='-T')
        noshow: bool = False

    def run_cluster(self, a):
        with h5py.File('data/slide_features.h5', 'r') as f:
            features = f['features'][:]
            df = pd.DataFrame({
                'name': [int((v.decode('utf-8'))) for v in f['names'][:]],
                'filename': [v.decode('utf-8') for v in f['filenames'][:]],
                'order': f['orders'][:],
            })

        df_clinical = pd.read_excel('./data/clinical_data_cleaned.xlsx', index_col=0)
        df = pd.merge(
            df,
            df_clinical,
            left_on='name',
            right_index=True,
            how='left'
        )

        print('Loaded features', features.shape)
        scaler = StandardScaler()
        scaled_features = scaler.fit_transform(features)
        # scaled_features = features

        print('UMAP fitting...')
        reducer = umap.UMAP(
                n_neighbors=10,
                min_dist=0.05,
                n_components=2,
                metric='cosine',
                random_state=a.seed,
                n_jobs=1,
            )
        embedding = reducer.fit_transform(scaled_features)
        print('Loaded features', features.shape)

        if a.target in [
                'HDBSCAN',
                'CD10 IHC', 'MUM1 IHC', 'HANS', 'BCL6 FISH', 'MYC FISH', 'BCL2 FISH',
                'ECOG PS', 'LDH', 'EN', 'Stage', 'IPI Score',
                'IPI Risk Group (4 Class)', 'RIPI Risk Group', 'Follow-up Status',
                ]:
            mode = 'categorical'
        elif a.target in ['MYC IHC', 'BCL2 IHC', 'BCL6 IHC', 'Age', 'OS', 'PFS']:
            mode = 'numeric'
        else:
            raise RuntimeError('invalid target', a.target)


        plt.figure(figsize=(10, 8))
        marker_size = 15

        if mode == 'categorical':
            if a.target == 'cluster':
                eps = 0.2
                m = hdbscan.HDBSCAN(
                    min_cluster_size=5,
                    min_samples=5,
                    cluster_selection_epsilon=eps,
                    metric='euclidean',
                )
                labels = m.fit_predict(embedding)
                n_labels = len(set(labels)) - (1 if -1 in labels else 0)
            else:
                labels = df[a.target].fillna(-1)
                n_labels = len(set(labels))
            cmap = plt.cm.viridis

            noise_mask = labels == -1
            valid_labels = sorted(list(set(labels[~noise_mask])))
            norm = plt.Normalize(min(valid_labels or [0]), max(valid_labels or [1]))
            for label in valid_labels:
                mask = labels == label
                color = cmap(norm(label))
                plt.scatter(
                    embedding[mask, 0], embedding[mask, 1], c=[color],
                    s=marker_size, label=f'{a.target} {label}'
                )

            if np.any(noise_mask):
                plt.scatter(
                    embedding[noise_mask, 0], embedding[noise_mask, 1], c='gray',
                    s=marker_size, marker='x', label='Noise/NaN',
                )

        else:
            values = df[a.target]
            norm = Normalize(vmin=values.min(), vmax=values.max())
            values = values.fillna(-1)
            has_value = values > 0
            cmap = plt.cm.viridis
            scatter = plt.scatter(embedding[has_value, 0], embedding[has_value, 1], c=values[has_value],
                                  s=marker_size, cmap=cmap, norm=norm, label=a.target,)
            if np.any(has_value):
                plt.scatter(embedding[~has_value, 0], embedding[~has_value, 1], c='gray',
                            s=marker_size, marker='x', label='NaN')
            cbar = plt.colorbar(scatter)
            cbar.set_label(a.target)

        plt.title(f'UMAP + {a.target}')
        plt.xlabel('UMAP Dimension 1')
        plt.ylabel('UMAP Dimension 2')
        # plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
        plt.legend()
        plt.tight_layout()
        os.makedirs('out/umap', exist_ok=True)
        name = a.target.replace(' ', '_')
        plt.savefig(f'out/umap/umap_{name}.png')
        if not a.noshow:
            plt.show()

    class GlobalClusterArgs(CommonArgs):
        noshow: bool = False
        n_samples: int = 100

    def run_global_cluster(self, a):
        result = []

        with h5py.File('data/global_features.h5', 'r') as f:
            global_features = f['global_features'][:]
            lengths = f['lengths'][:]

        selected_features = []
        iii = []

        cursor = 0
        for l in lengths:
            slice = global_features[cursor:cursor+l]
            ii = np.random.choice(slice.shape[0], size=a.n_samples, replace=False)
            iii.append(ii)
            selected_features.append(slice[ii])
            cursor += l
        selected_features = np.concatenate(selected_features)

        features = selected_features

        print('Loaded features', features.dtype, features.shape)
        scaler = StandardScaler()
        scaled_features = scaler.fit_transform(features)

        reducer = umap.UMAP(
                n_neighbors=80,
                min_dist=0.3,
                n_components=2,
                metric='cosine',
                # random_state=a.seed
            )
        embedding = reducer.fit_transform(scaled_features)

        plt.scatter(embedding[:, 0], embedding[:, 1], s=1)
        plt.title(f'UMAP')
        plt.xlabel('UMAP Dimension 1')
        plt.ylabel('UMAP Dimension 2')
        # plt.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
        plt.tight_layout()
        plt.show()


    class ImageHistArgs(CommonArgs):
        input_path: str = Field(..., l='--in', s='-i')


    def run_image_hist(self, a):
        img = cv2.imread(a.input_path)

        # BGRからRGBとHSVに変換
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)

        print(is_white_patch_std_sat(rgb, verbose=True))

        # 8つのサブプロットを作成 (4x3)
        fig, axes = plt.subplots(3, 4, figsize=(20, 10))

        # RGBヒストグラム
        for i, color in enumerate(['r', 'g', 'b']):
            # ヒストグラムを計算
            hist = cv2.calcHist([rgb], [i], None, [256], [0, 256])
            # プロット
            axes[0, i].plot(hist, color=color)
            axes[0, i].set_xlim([0, 256])
            axes[0, i].set_xticks(range(0, 257, 10))  # 10刻みでメモリを設定
            axes[0, i].set_title(f'{color.upper()} Histogram')
            axes[0, i].grid(True)

        # RGB平均値ヒストグラム（グレースケール）
        kernel_size = 3
        mean_rgb = cv2.blur(rgb, (kernel_size, kernel_size))

        # 各ピクセルでRGBの平均を計算してグレースケールに変換
        gray_from_rgb = np.mean(mean_rgb, axis=2).astype(np.uint8)

        # グレースケール画像のヒストグラムを計算
        gray_hist = cv2.calcHist([gray_from_rgb], [0], None, [256], [0, 256])

        # ヒストグラムをプロット
        axes[0, 3].plot(gray_hist, color='gray')
        axes[0, 3].set_xlim([0, 256])
        axes[0, 3].set_title('Grayscale (RGB Mean) Histogram')
        axes[0, 3].grid(True)

        # HSVヒストグラム
        colors = ['r', 'g', 'b']  # プロット用の色（実際のHSVとは無関係）
        titles = ['Hue', 'Saturation', 'Value']
        ranges = [[0, 180], [0, 256], [0, 256]]  # H: 0-179, S: 0-255, V: 0-255
        for i in range(3):
            # ヒストグラムを計算
            hist = cv2.calcHist([hsv], [i], None, [ranges[i][1]], ranges[i])
            # プロット
            axes[1, i].plot(hist, color=colors[i])
            axes[1, i].set_xlim(ranges[i])
            axes[1, i].set_xticks(range(0, ranges[i][1] + 1, 10))
            axes[1, i].set_title(f'{titles[i]} Histogram')
            axes[1, i].grid(True)

        # RGB標準偏差ヒストグラム
        # 標準偏差を計算
        mean_squared = cv2.blur(np.square(rgb.astype(np.float32)), (kernel_size, kernel_size))
        squared_mean = np.square(mean_rgb.astype(np.float32))
        std_rgb = np.sqrt(np.maximum(0, mean_squared - squared_mean)).astype(np.uint8)

        # RGBチャンネルの標準偏差の平均を計算
        std_gray = np.mean(std_rgb, axis=2).astype(np.uint8)

        # 表示幅を調整
        max_std_value = np.max(std_gray)
        histogram_range = [0, 50]

        # ヒストグラムを計算
        std_hist = cv2.calcHist([std_gray], [0], None, [max_std_value+1], histogram_range)

        # ヒストグラムをプロット
        axes[1, 3].plot(std_hist, color='orange')
        axes[1, 3].set_xlim(histogram_range)
        axes[1, 3].set_title(f'RGB Std Histogram (Range: 0-{max_std_value})')
        axes[1, 3].grid(True)


        # LABヒストグラム (3段目)
        lab_colors = ['k', 'g', 'b']  # プロット用の色（L:黒, a:緑, b:青）
        lab_titles = ['Lightness', 'a (green-red)', 'b (blue-yellow)']
        lab_ranges = [[0, 256], [0, 256], [0, 256]]  # L: 0-255, a: 0-255, b: 0-255

        for i in range(3):
            # ヒストグラムを計算
            hist = cv2.calcHist([lab], [i], None, [256], [0, 256])
            # プロット
            axes[2, i].plot(hist, color=lab_colors[i])
            axes[2, i].set_xlim([0, 256])
            axes[2, i].set_xticks(range(0, 257, 10))
            axes[2, i].set_title(f'LAB {lab_titles[i]} Histogram')
            axes[2, i].grid(True)

        # LAB平均値ヒストグラム
        mean_lab = cv2.blur(lab, (kernel_size, kernel_size))
        # 各ピクセルでLABの平均を計算
        lab_mean = np.mean(mean_lab, axis=2).astype(np.uint8)
        # ヒストグラムを計算
        lab_mean_hist = cv2.calcHist([lab_mean], [0], None, [256], [0, 256])
        # ヒストグラムをプロット
        axes[2, 3].plot(lab_mean_hist, color='purple')
        axes[2, 3].set_xlim([0, 256])
        axes[2, 3].set_title('LAB Mean Histogram')
        axes[2, 3].grid(True)

        plt.tight_layout()
        plt.show()

if __name__ == '__main__':
    cli = CLI()
    cli.run()
