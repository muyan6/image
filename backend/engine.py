import logging
import os

import cv2
import numpy as np

log = logging.getLogger("rescue.engine")

# 逆向提取自原版「猫猫九命机」标定图像的 20 维色彩影调基底权重
BASE_STYLE_WEIGHTS = np.array([
    [0.0428897436, 0.0325100379, 0.0243440636],
    [0.0776181816, -0.4853369529, -0.1994384134],
    [0.0414318750, 0.3219102088, -1.3856540444],
    [0.1360763053, 0.5529674546, 2.1678357985],
    [1.9047668906, 0.1360923072, 1.9214381445],
    [-1.8906982990, -2.1059541219, 8.8717256773],
    [2.2062135011, -0.8001844190, -7.0281539984],
    [4.1901529325, 5.4790597368, -7.4714241292],
    [-2.0435057857, 0.1883552569, -0.8131075263],
    [-3.0102864880, -1.8225225202, 5.2162301502],
    [-1.7094132083, -1.0730276271, -9.2215266545],
    [-8.0537951461, -4.2132038782, 11.5643859288],
    [0.6903090886, -1.2238800819, -14.7909548967],
    [-4.4370943287, -1.5609448688, 31.2010816491],
    [6.3658104030, 1.9570589839, -22.6491453219],
    [8.0621667093, 1.5049785393, -19.4983060368],
    [-1.9532228868, 3.1021443639, 32.1667785503],
    [-6.8188605742, -2.5982721971, 17.7812552690],
    [2.9817728518, -0.2563320938, -4.5825342911],
    [4.1330593263, 3.7925072493, -22.3234657893]
], dtype=np.float32)

class ImageRescueEngine:
    """
    全自动自适应智能废片拯救引擎 (Adaptive Photo Rescue Engine)：
    原理：任何照片之所以成为“废片”，无非是以下 4 类问题组合：
      1. 灰蒙蒙 / 镜头眩光 -> 黑电平浮空 (P1 > 15)
      2. 太黄 / 偏色 -> 色温与白平衡偏移 (LAB 空间 b 轴异常)
      3. 太暗 / 欠曝 -> 暗部死黑、缺乏层次 (平均亮度 L < 100)
      4. 扁平无质感 -> 缺乏中频微反差与发丝细节 (高频能量低)

    本引擎通过每张图实时的全维直方图与色彩空间诊断，动态计算专属这副照片的修复参数方程，
    保证换任何图片（暗光、逆光、夜景、人像、户外）都能智能自适应拯救！
    """

    def __init__(self, lut_dir=None):
        self.lut_dir = lut_dir or os.path.join(os.path.dirname(__file__), "luts")
        os.makedirs(self.lut_dir, exist_ok=True)

    @staticmethod
    def poly_features(rgb_norm):
        """展开 20 维多项式色彩空间基函数"""
        r = rgb_norm[:, 0]
        g = rgb_norm[:, 1]
        b = rgb_norm[:, 2]
        return np.column_stack([
            np.ones_like(r),
            r, g, b,
            r**2, g**2, b**2,
            r*g, g*b, b*r,
            r**3, g**3, b**3,
            r*r*g, r*g*g, g*g*b, g*b*b, b*b*r, b*r*r, r*g*b
        ])

    def adaptive_rescue(self, img_bgr):
        """核心自适应诊断与处理链路"""
        h, w = img_bgr.shape[:2]
        img = img_bgr.astype(np.float32) / 255.0

        # ========== 阶段 1：全图自适应场景诊断 (SCENE DIAGNOSIS) ==========
        lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        l, a, b = cv2.split(lab)
        
        l_mean = float(np.mean(l))
        l_p1 = float(np.percentile(l, 1.0))   # 极暗部 1% 阈值
        l_p99 = float(np.percentile(l, 99.0)) # 高光 99% 阈值
        l_std = float(np.std(l))              # 反差度
        b_mean = float(np.mean(b))            # 黄蓝偏色轴（128为中性，>128偏黄）
        a_mean = float(np.mean(a))            # 红绿偏色轴（128为中性）

        log.debug("诊断: 亮度均值=%.1f 暗电平=%.1f 反差度=%.1f 黄蓝轴=%.1f",
                  l_mean, l_p1, l_std, b_mean)

        # ========== 阶段 2：自适应黑电平沉降与去灰 (DE-HAZE) ==========
        # 如果暗部浮空（如镜头油污、杂光漫反射导致黑色发灰），自适应下沉黑电平
        if l_p1 > 8.0:
            # 动态计算沉黑偏移量，消除发灰感
            black_offset = min(0.18, (l_p1 - 8.0) * 0.80 / 255.0)
            img = np.clip((img - black_offset) / (1.0 - black_offset + 1e-5), 0.0, 1.0)
            log.debug("去灰: 黑位沉降 offset=%.3f", black_offset)

        # ========== 阶段 3：自适应偏色中和与白平衡校准 (WHITE BALANCE) ==========
        yellow_cast = b_mean - 128.0
        if yellow_cast > 3.0:
            cool_ratio = float(np.clip(yellow_cast / 18.0, 0.20, 0.75))
            b_ch, g_ch, r_ch = cv2.split(img)
            b_ch = np.clip(b_ch * (1.0 + 0.16 * cool_ratio), 0.0, 1.0).astype(np.float32)
            r_ch = np.clip(r_ch * (1.0 - 0.07 * cool_ratio), 0.0, 1.0).astype(np.float32)
            g_ch = g_ch.astype(np.float32)
            img = cv2.merge([b_ch, g_ch, r_ch])
            log.debug("去黄: 强度=%.2f", cool_ratio)
        elif yellow_cast < -5.0:
            warm_ratio = float(np.clip(-yellow_cast / 18.0, 0.20, 0.60))
            b_ch, g_ch, r_ch = cv2.split(img)
            r_ch = np.clip(r_ch * (1.0 + 0.10 * warm_ratio), 0.0, 1.0).astype(np.float32)
            b_ch = b_ch.astype(np.float32)
            g_ch = g_ch.astype(np.float32)
            img = cv2.merge([b_ch, g_ch, r_ch])
            log.debug("补暖: 强度=%.2f", warm_ratio)

        # ========== 阶段 4：自适应动态光影与阴影唤醒 (SHADOW RECOVERY) ==========
        gray = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        if l_mean < 115.0:
            shadow_intensity = float(np.clip((115.0 - l_mean) / 80.0, 0.15, 0.50))
            shadow_mask = np.clip(1.0 - gray * 1.5, 0.0, 1.0) ** 1.15
            img = np.clip(img + shadow_intensity * shadow_mask[:, :, np.newaxis] * 0.28, 0.0, 1.0).astype(np.float32)
            log.debug("提亮: 强度=%.2f", shadow_intensity)

        # ========== 阶段 5：自适应 S 曲线微反差塑造 (TONAL DIMENSIONALITY) ==========
        contrast = float(np.clip(1.10 + (48.0 - min(48.0, l_std)) * 0.004, 1.05, 1.25))
        img = np.clip(0.5 + contrast * (img - 0.5) + 0.06 * np.sin(2 * np.pi * (img - 0.25)), 0.0, 1.0).astype(np.float32)

        # ========== 阶段 6：高维基底影调自适应融合 (ATMOSPHERE BLEND) ==========
        rgb_in = img[:, :, ::-1].reshape(-1, 3)
        feats = self.poly_features(rgb_in)
        pred_rgb = np.clip(feats @ BASE_STYLE_WEIGHTS, 0.0, 1.0)
        base_bgr = (pred_rgb[:, ::-1] * 255.0).reshape(h, w, 3).astype(np.float32) / 255.0

        blend_weight = float(np.clip(0.40 + (yellow_cast / 30.0) * 0.25 + (l_p1 / 50.0) * 0.20, 0.35, 0.70))
        img = cv2.addWeighted(base_bgr, blend_weight, img, 1.0 - blend_weight, 0.0).astype(np.float32)

        # ========== 阶段 7：自然饱和度与人像肤色保护 (VIBRANCE & SKIN) ==========
        hsv = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
        h_ch, s_ch, v_ch = cv2.split(hsv)
        s_norm = s_ch / 255.0
        skin_mask = np.clip(1.0 - np.abs(h_ch - 16.0) / 14.0, 0.0, 1.0)
        vibrance_boost = 1.0 + (0.22 * (1.0 - s_norm) * (1.0 - 0.65 * skin_mask))
        s_ch = np.clip(s_ch * vibrance_boost, 0.0, 255.0).astype(np.float32)
        img_graded = cv2.cvtColor(cv2.merge([h_ch.astype(np.float32), s_ch, v_ch.astype(np.float32)]).astype(np.uint8), cv2.COLOR_HSV2BGR)

        # ========== 阶段 8：自适应中频质感清晰度 (CLARITY / TEXTURE) ==========
        blur = cv2.GaussianBlur(img_graded, (0, 0), 2.0)
        clarity_strength = 0.35 if l_std > 35 else 0.45
        crisp = cv2.addWeighted(img_graded, 1.0 + clarity_strength, blur, -clarity_strength, 0.0)
        final_bgr = np.clip(crisp, 0, 255).astype(np.uint8)

        return final_bgr

    @staticmethod
    def apply_style(img_bgr, style=None):
        """摄影风格调色。

        style 为 None / "ai" / "none" 时原样返回 —— 走 AI 通道时不该再叠一层，
        否则等于在模型输出上二次调色。
        """
        if not style or style in ("ai", "none"):
            return img_bgr

        img = img_bgr.astype(np.float32) / 255.0
        b, g, r = img[:, :, 0], img[:, :, 1], img[:, :, 2]

        if style == "gym_contrast":
            # 力量高反差：压暗部杂色，提亮部金属反光，黑金对比
            r = np.power(r, 0.96) * 1.04
            g = np.power(g, 0.98)
            b = np.power(b, 1.04) * 0.96
            c = 1.12
            r, g, b = (0.5 + (ch - 0.5) * c for ch in (r, g, b))
        elif style == "clear":
            # 冷白通透：去油腻暗黄，提亮面部
            r = np.power(r, 0.90) * 1.02
            g = np.power(g, 0.92) * 1.02
            b = np.power(b, 0.86) * 1.08
        elif style == "fuji":
            # 富士经典：暗部青灰、暖调高光、低饱和
            r = np.power(r, 0.95) * 1.03
            g = np.power(g, 0.98)
            b = np.power(b, 1.02) * 0.95
        elif style == "vintage_film":
            # 柯达暖金胶片
            r = np.power(r, 0.92) * 1.06
            g = np.power(g, 0.96) * 1.02
            b = np.power(b, 1.06) * 0.88
        else:
            return img_bgr

        return np.clip(cv2.merge([b, g, r]) * 255.0, 0, 255).astype(np.uint8)

    def process(self, input_path, output_path, quality="fine",
                upscale_2k=True, style=None):
        """执行全自动自适应废片拯救。

        upscale_2k=False 时跳过 2K 上采样。调用方已经在前面做过归一化
        （长边 1536）时应当关掉，否则会把 1536 又插值回 2000，纯属放大噪声。
        style 只在本地通道生效，AI 通道的输出不再叠加风格。
        """
        img_bgr = cv2.imread(input_path)
        if img_bgr is None:
            raise ValueError("无法读取图片: %s" % input_path)

        # 核心全自动自适应诊断与光影重构
        rescued = self.adaptive_rescue(img_bgr)

        # 风格调色（仅本地通道；AI 输出不再叠加）
        rescued = self.apply_style(rescued, style)

        h, w = rescued.shape[:2]

        if quality == "fine" and upscale_2k:
            # 精细 2K 模式：保证长边至少 2000px
            max_edge = max(h, w)
            if max_edge < 2000:
                scale = 2000.0 / max_edge
                rescued_2k = cv2.resize(
                    rescued,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_LANCZOS4,
                )
                blur_2k = cv2.GaussianBlur(rescued_2k, (0, 0), 1.5)
                rescued = np.clip(
                    cv2.addWeighted(rescued_2k, 1.15, blur_2k, -0.15, 0.0),
                    0, 255,
                ).astype(np.uint8)

        ok, buf = cv2.imencode(
            os.path.splitext(output_path)[1].lower() or ".jpg",
            rescued,
            [int(cv2.IMWRITE_JPEG_QUALITY), 95],
        )
        if not ok:
            raise RuntimeError("结果编码失败")
        buf.tofile(output_path)
        return output_path
