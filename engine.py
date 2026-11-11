"""CPU-only, strictly local image operations.

ImageEngine(root).status() -> engine readiness.
cutout(input_path, output_path, threshold=245) -> warning strings.
split(input_path, output_dir, min_height=250, max_height=1600,
      threshold=.97, cuts=None) -> {segments: [{path, box}], cuts, warnings}.
composite(product_path, background_path, output_path, params) -> warnings.
auto_settings(product_path, background_path, params) -> {light, shadow, note}.
layers(input_path, output_dir, repair=False) -> {layers, warnings}.

All layer PNGs have the original full canvas. Bounds are x/y/width/height.
No inference component downloads a model, contacts a server or uses a GPU.
Existing output files are never replaced. All pixel coordinates follow EXIF
orientation as displayed. Composite scale refers to the full source width,
including transparent margins; x/y locate the full source center.
"""

from __future__ import annotations

import importlib.util
import hashlib
import math
import os
import threading
from statistics import median
from array import array
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps


MAX_PIXELS = 120_000_000
MAX_LAYER_PIXELS = 32_000_000
U2NET_SIZE = 175_997_641
U2NET_MD5 = "60024c5c889badc19c04ad937298a77b"
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _number(value, default, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(lo, min(hi, value))


def _load(path):
    with Image.open(path) as opened:
        if opened.width * opened.height > MAX_PIXELS:
            raise ValueError("图片超过 1.2 亿像素，请先按版块切分。")
        image = ImageOps.exif_transpose(opened)
        return image.convert("RGBA")


def _save(image, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        image.save(target, "PNG")
    return str(path)


def _bounds(mask):
    box = mask.getbbox()
    if box is None:
        return {"x": 0, "y": 0, "width": 0, "height": 0}
    return {"x": box[0], "y": box[1], "width": box[2] - box[0], "height": box[3] - box[1]}


class ImageEngine:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.model_dir = self.root / "models"
        self._cutout_session = None
        self._ocr_engine = None
        self._lock = threading.RLock()
        self._verified_model = None

    @staticmethod
    def _available(module):
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            return False

    def _ocr_models(self):
        """Explicit local models win; otherwise use models bundled in the wheel."""
        candidates = [self.model_dir / "ocr", self.model_dir]
        try:
            spec = importlib.util.find_spec("rapidocr_onnxruntime")
            if spec and spec.origin:
                candidates.append(Path(spec.origin).parent / "models")
        except (ImportError, ValueError, ModuleNotFoundError):
            pass
        for directory in candidates:
            if not directory.is_dir():
                continue
            files = list(directory.glob("*.onnx"))
            found = {}
            for kind in ("det", "rec", "cls"):
                matching = [p for p in files if kind in p.stem.lower()]
                if matching:
                    found[kind] = matching[0]
            if len(found) == 3:
                return found
        return {}

    def _model_ready(self):
        model = self.model_dir / "u2net.onnx"
        try:
            stat = model.stat()
            if stat.st_size != U2NET_SIZE:
                return False
            with self._lock:
                if self._verified_model is None or self._verified_model[0] != stat.st_mtime_ns:
                    with model.open("rb") as f:
                        matches = hashlib.file_digest(f, "md5").hexdigest() == U2NET_MD5
                    self._verified_model = (stat.st_mtime_ns, matches)
                return self._verified_model[1]
        except OSError:
            return False

    def status(self):
        neural = self._available("numpy") and self._available("onnxruntime") and self._model_ready()
        ocr = bool(self._ocr_models()) and self._available("rapidocr_onnxruntime")
        warnings = []
        if not neural:
            warnings.append("智能抠图模型尚未就绪；当前可用白底边缘识别与手动修边。")
        if not ocr:
            warnings.append("文字识别组件尚未就绪；图片分层暂不自动识别文字。")
        return {
            "name": "离线产品图工作台",
            "engine": {"cutout": neural, "ocr": ocr, "basicCutout": True, "composite": True, "split": True},
            "models": {"cutout": "u2net" if neural else "white-edge", "ocr": "RapidOCR 中英文本地模型" if ocr else None},
            "warning": " ".join(warnings),
            "offline": True,
        }

    def _session(self):
        if self._cutout_session is not None:
            return self._cutout_session
        model = self.model_dir / "u2net.onnx"
        if not self._model_ready():
            raise RuntimeError("本地抠图模型尚未完整就绪。")
        # Load only the locally verified graph. The ONNX API accepts this
        # file directly and requires neither model fetching nor JIT libraries.
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        opts.inter_op_num_threads = 1
        self._cutout_session = ort.InferenceSession(str(model), sess_options=opts, providers=["CPUExecutionProvider"])
        return self._cutout_session

    def _neural_mask(self, image):
        import numpy as np
        session = self._session()
        # U2Net reference preprocessing uses a 320-square RGB input, image
        # maximum normalization, then channel-wise ImageNet normalization.
        rgb = image.convert("RGB").resize((320, 320), Image.Resampling.LANCZOS)
        tensor = np.asarray(rgb, dtype=np.float32)
        tensor /= max(float(tensor.max()), 1.0)
        tensor -= np.asarray([.485, .456, .406], dtype=np.float32)
        tensor /= np.asarray([.229, .224, .225], dtype=np.float32)
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        name = session.get_inputs()[0].name
        prediction = session.run([session.get_outputs()[0].name], {name: tensor})[0][0, 0]
        lo, hi = float(prediction.min()), float(prediction.max())
        if not math.isfinite(lo + hi) or hi - lo < 1e-8:
            raise RuntimeError("未识别到可分离的主体。")
        opacity = np.clip((prediction - lo) * (255 / (hi - lo)), 0, 255).astype(np.uint8)
        return Image.fromarray(opacity).resize(image.size, Image.Resampling.LANCZOS)

    def _ocr(self):
        if self._ocr_engine is not None:
            return self._ocr_engine
        models = self._ocr_models()
        if len(models) != 3:
            raise RuntimeError("本地文字识别模型不完整。")
        from rapidocr_onnxruntime import RapidOCR
        self._ocr_engine = RapidOCR(
            det_model_path=str(models["det"]),
            rec_model_path=str(models["rec"]),
            cls_model_path=str(models["cls"]),
            det_use_cuda=False, rec_use_cuda=False, cls_use_cuda=False,
            print_verbose=False,
        )
        return self._ocr_engine

    @staticmethod
    def _basic_mask(image, threshold):
        """Remove only near-white regions connected to the image boundary.

        Unlike a global white-key, closed white reflections inside metal stay
        opaque. Enclosed handle holes deliberately require manual refinement.
        """
        threshold = _number(threshold, 245, 0, 254)
        if threshold <= 1:
            threshold *= 255
        white = int(_number(threshold, 245, 180, 254))
        small = image.convert("RGB")
        small.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        w, h = small.size
        pixels = small.tobytes()
        candidate = bytearray(w * h)
        for index in range(w * h):
            r, g, b = pixels[index * 3:index * 3 + 3]
            if min(r, g, b) >= white and max(r, g, b) - min(r, g, b) <= 28:
                candidate[index] = 1
        background = bytearray(w * h)
        queue = array("I")
        for x in range(w):
            for index in (x, (h - 1) * w + x):
                if candidate[index] and not background[index]:
                    background[index] = 255
                    queue.append(index)
        for y in range(h):
            for index in (y * w, y * w + w - 1):
                if candidate[index] and not background[index]:
                    background[index] = 255
                    queue.append(index)
        head = 0
        while head < len(queue):
            index = queue[head]
            head += 1
            x = index % w
            neighbors = []
            if x: neighbors.append(index - 1)
            if x + 1 < w: neighbors.append(index + 1)
            if index >= w: neighbors.append(index - w)
            if index + w < w * h: neighbors.append(index + w)
            for next_index in neighbors:
                if candidate[next_index] and not background[next_index]:
                    background[next_index] = 255
                    queue.append(next_index)
        mask = ImageOps.invert(Image.frombytes("L", (w, h), bytes(background)))
        if mask.size != image.size:
            mask = mask.resize(image.size, Image.Resampling.LANCZOS)
        return mask

    def _mask(self, image, threshold=245):
        alpha = image.getchannel("A")
        if alpha.getextrema()[0] < 255:
            return alpha, []
        warnings = []
        if self.status()["engine"]["cutout"]:
            try:
                with self._lock:
                    mask = self._neural_mask(image)
                warnings.append("已用本地模型分离主体；透明玻璃、金属高光和孔洞请放大检查并修边。")
                return mask, warnings
            except Exception as exc:
                warnings.append("本地智能抠图未成功，已改用白底边缘识别：" + str(exc)[:180])
        warnings.append("使用白底边缘识别，仅去除与边缘连通的近白背景；复杂背景、封闭孔洞及玻璃需要手动修边。")
        return self._basic_mask(image, threshold), warnings

    def cutout(self, input_path, output_path, threshold=245):
        image = _load(input_path)
        mask, warnings = self._mask(image, threshold)
        image.putalpha(mask)
        _save(image, output_path)
        return warnings

    def split(self, input_path, output_dir, min_height=250, max_height=1600, threshold=.97, cuts=None):
        image = _load(input_path)
        w, h = image.size
        min_height = int(_number(min_height, 250, 32, 12000))
        max_height = int(_number(max_height, 1600, min_height, 30000))
        threshold = _number(threshold, .97, .5, 1)
        warnings = []
        if cuts is not None:
            if not isinstance(cuts, list) or len(cuts) > 500:
                raise ValueError("手动切线必须是最多 500 个纵向像素位置。")
            try:
                chosen = sorted(set(int(v) for v in cuts))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("切线位置必须为整数。")
            if any(y <= 0 or y >= h for y in chosen):
                raise ValueError("切线必须位于图片内部。")
        else:
            sw = min(320, w)
            sh = min(30000, h)
            sampled = image.convert("RGB").resize((sw, sh), Image.Resampling.BOX)
            data = sampled.tobytes()
            quiet = []
            strong_gutters = []
            run_start = None
            # A blank row can be white or a consistent colored section gutter.
            for y in range(sh):
                row = data[y * sw * 3:(y + 1) * sw * 3]
                edge_indices = (0, min(sw - 1, 3), max(0, sw - 4), sw - 1)
                color = tuple(sorted(row[x * 3 + c] for x in edge_indices)[1] for c in range(3))
                matching = 0
                for x in range(sw):
                    j = x * 3
                    if max(abs(row[j + c] - color[c]) for c in range(3)) <= 13:
                        matching += 1
                blank = matching / sw >= threshold
                if blank and run_start is None:
                    run_start = y
                if (not blank or y == sh - 1) and run_start is not None:
                    end = y if not blank else y + 1
                    gutter_height = (end - run_start) * h / sh
                    if run_start > 0 and end < sh and gutter_height >= 8:
                        middle = int((run_start + end) * h / sh / 2)
                        quiet.append(middle)
                        if gutter_height >= max(15, min(w * .025, 45)):
                            strong_gutters.append(middle)
                    run_start = None
            chosen = []
            start = 0
            forced = 0
            while h - start >= min_height * 2 or h - start > max_height:
                def eligible(y):
                    return start + min_height <= y <= start + max_height and h - y >= min_height
                strong = next((y for y in strong_gutters if eligible(y)), None)
                if strong is not None:
                    end = strong
                elif h - start <= max_height:
                    break
                else:
                    options = [y for y in quiet if eligible(y)]
                    if options:
                        end = max(options)
                    else:
                        end = start + max_height
                        forced += 1
                chosen.append(end)
                start = end
            if chosen and h - chosen[-1] < min_height:
                previous = chosen[-2] if len(chosen) > 1 else 0
                if h - previous <= max_height:
                    chosen.pop()
            if forced:
                warnings.append(f"有 {forced} 处未找到明显分隔带，暂按最大高度切分，请检查切线是否穿过文字或产品。")
            warnings.append("自动切线根据留白推测版块；可输入纵向像素位置重新切分。")
        if len(chosen) > 500:
            raise ValueError("分段过多，请增大分段高度。")
        output_dir = Path(output_dir)
        segments = []
        points = [0] + chosen + [h]
        for index, (y1, y2) in enumerate(zip(points, points[1:]), 1):
            box = [0, y1, w, y2]
            dest = output_dir / f"segment-{index:03d}.png"
            _save(image.crop(tuple(box)), dest)
            segments.append({"path": str(dest), "box": box})
        return {"segments": segments, "cuts": chosen, "warnings": warnings}

    def auto_settings(self, product_path, background_path, params):
        """Estimate gentle color corrections and the contact-shadow direction.

        The background is sampled around the product's actual visible base at
        the chosen transform. Neutral product samples exclude very bright
        highlights and colored details such as a brown handle. Bounds limit
        adjustments because reflectance and the illuminant are ambiguous.
        """
        product = _load(product_path)
        w = int(_number(params.get("width"), 1000, 128, 6000))
        h = int(_number(params.get("height"), 1000, 128, 6000))
        preview_scale = 256 / max(w, h)
        sw, sh = max(1, round(w * preview_scale)), max(1, round(h * preview_scale))
        if background_path:
            background = ImageOps.fit(_load(background_path), (sw, sh), Image.Resampling.LANCZOS)
            flat = Image.new("RGBA", background.size, "white")
            flat.alpha_composite(background)
            background = flat.convert("RGB")
        else:
            background = Image.new("RGB", (sw, sh), "white")
        product.thumbnail((220, 220), Image.Resampling.LANCZOS)
        alpha = product.getchannel("A")
        if alpha.getextrema()[0] == 255:
            guessed = self._basic_mask(product, 245)
            if guessed.getbbox():
                product.putalpha(guessed)
        rotation = math.radians(_number(params.get("rotation"), 0, -180, 180))
        sine, cosine = math.sin(rotation), math.cos(rotation)
        factor = _number(params.get("scale"), .75, .02, 3) * sw / product.width
        cx = sw * _number(params.get("x"), .5, -1, 2)
        cy = sh * _number(params.get("y"), .55, -1, 2)
        points = []
        product_neutrals = []
        for i, (r, g, b, a) in enumerate(product.getdata()):
            if a < 150:
                continue
            dx = (i % product.width + .5 - product.width / 2) * factor
            dy = (i // product.width + .5 - product.height / 2) * factor
            points.append((cx + cosine * dx - sine * dy, cy + sine * dx + cosine * dy))
            y = .213 * r + .715 * g + .072 * b
            if 60 < y < 240 and max(r, g, b) - min(r, g, b) <= 26:
                product_neutrals.append((r - b, y))
        if not points:
            raise ValueError("产品图完全透明，请先恢复产品区域。")
        bottom = max(y for x, y in points)
        visible_h = max(1, bottom - min(y for x, y in points))
        base = [x for x, y in points if y >= bottom - max(2, visible_h * .12)]
        footprint = max(sw * .12, max(base) - min(base))
        center = max(0, min(sw - 1, median(base)))
        floor_y = max(0, min(sh - 1, bottom))
        half_width = max(4, footprint * .7)
        half_height = max(3, min(sh * .13, visible_h * .18))
        x1, x2 = max(0, int(center - half_width)), min(sw, math.ceil(center + half_width + 1))
        y1, y2 = max(0, int(floor_y - half_height)), min(sh, math.ceil(floor_y + half_height + 1))
        tone = []
        neutrals = []
        left, right = [], []
        px = background.load()
        for y in range(y1, y2):
            for x in range(x1, x2):
                r, g, b = px[x, y]
                luminance = .213 * r + .715 * g + .072 * b
                tone.append(luminance)
                if x < center - half_width * .15:
                    left.append(luminance)
                elif x > center + half_width * .15:
                    right.append(luminance)
                if 60 < luminance < 249 and max(r, g, b) - min(r, g, b) <= 32:
                    neutrals.append(r - b)
        tone.sort()
        q10 = tone[round((len(tone) - 1) * .1)]
        q90 = tone[round((len(tone) - 1) * .9)]
        surface = median(tone)
        brightness = _number(1 + (surface - 195) / 1200, 1, .92, 1.045)
        if product_neutrals:
            highlights = sorted(y for _, y in product_neutrals)
            p90 = highlights[round((len(highlights) - 1) * .9)]
            brightness = min(brightness, max(1, 240 / max(1, p90)))
        contrast = _number(.985 + (q90 - q10) / 2700, 1, .985, 1.035)
        warmth = 0
        if len(neutrals) >= max(8, len(tone) * .1) and len(product_neutrals) > 8:
            difference = median(neutrals) - median(color for color, _ in product_neutrals)
            warmth = round(_number(difference * .55, 0, -12, 12))
        lateral = median(left) - median(right) if left and right else 0
        if abs(lateral) < 9:
            lateral = 0
        size = min(w, h)
        note = "按产品落点附近的背景估算亮度和色温。"
        if lateral:
            note += "背景左侧较亮，阴影略向右延伸。" if lateral > 0 else "背景右侧较亮，阴影略向左延伸。"
        else:
            note += "背景光向不明显，已采用贴近锅底的柔和阴影。"
        return {
            "light": {"brightness": round(brightness, 3), "contrast": round(contrast, 3), "saturation": 1, "warmth": warmth},
            "shadow": {
                "opacity": round(_number(.17 + (255 - surface) / 1500 + abs(lateral) / 2600, .22, .16, .32), 3),
                "blur": round(_number(size * (.015 + .008 * (1 - min(1, abs(lateral) / 100))), 18, 2, 100)),
                "offsetX": round(_number(lateral / 255 * size * .085, 0, -90, 90)),
                "offsetY": round(_number(size * .008, 8, 1, 60)),
                "spread": 1,
            },
            "note": note,
        }

    def composite(self, product_path, background_path, output_path, params):
        product = _load(product_path)
        width = int(_number(params.get("width"), 1000, 128, 6000))
        height = int(_number(params.get("height"), 1000, 128, 6000))
        if width * height > 24_000_000:
            raise ValueError("合成画布请控制在 2400 万像素以内。")
        warnings = []
        if product.getchannel("A").getextrema()[0] == 255:
            mask, notes = self._mask(product)
            product.putalpha(mask)
            warnings.extend(notes)
        canvas = Image.new("RGBA", (width, height), "white")
        if background_path:
            background = ImageOps.fit(_load(background_path), canvas.size, Image.Resampling.LANCZOS, centering=(.5, .5))
            canvas.alpha_composite(background)
        light = params.get("light") or {}
        brightness = _number(light.get("brightness"), 1, .25, 2)
        contrast = _number(light.get("contrast"), 1, .25, 2)
        saturation = _number(light.get("saturation"), 1, 0, 2)
        warmth = _number(light.get("warmth"), 0, -100, 100)
        alpha = product.getchannel("A")
        rgb = product.convert("RGB")
        if brightness != 1: rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
        if contrast != 1: rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
        if saturation != 1: rgb = ImageEnhance.Color(rgb).enhance(saturation)
        if warmth:
            red, green, blue = rgb.split()
            delta = warmth * .3
            red = red.point([int(max(0, min(255, x + delta))) for x in range(256)])
            blue = blue.point([int(max(0, min(255, x - delta))) for x in range(256)])
            rgb = Image.merge("RGB", (red, green, blue))
        product = rgb.convert("RGBA")
        product.putalpha(alpha)
        scale = _number(params.get("scale"), .75, .02, 3)
        target_w = max(1, round(width * scale))
        target_h = max(1, round(target_w * product.height / product.width))
        if target_w * target_h > 32_000_000:
            raise ValueError("产品缩放过大，请减小比例或先裁去多余透明区域。")
        product = product.resize((target_w, target_h), Image.Resampling.LANCZOS)
        rotation = _number(params.get("rotation"), 0, -180, 180)
        if rotation:
            product = product.rotate(-rotation, Image.Resampling.BICUBIC, expand=True)
        cx = width * _number(params.get("x"), .5, -1, 2)
        cy = height * _number(params.get("y"), .55, -1, 2)
        px, py = round(cx - product.width / 2), round(cy - product.height / 2)
        alpha = product.getchannel("A")
        box = alpha.getbbox()
        if box is None:
            raise ValueError("产品图完全透明，请恢复产品区域后再合成。")
        if px + box[0] < 0 or py + box[1] < 0 or px + box[2] > width or py + box[3] > height:
            warnings.append("部分产品已超出画布，请调整位置或缩小比例。")
        shadow = params.get("shadow") or {}
        opacity = _number(shadow.get("opacity"), .22, 0, .9)
        blur = _number(shadow.get("blur"), 18, 0, 180)
        dx = _number(shadow.get("offsetX"), 0, -1000, 1000)
        dy = _number(shadow.get("offsetY"), 12, -1000, 1000)
        spread = _number(shadow.get("spread"), 1, .3, 3)
        if opacity:
            silhouette = alpha.crop(box)
            shadow_width = max(1, round(silhouette.width * spread))
            shadow_height = max(2, round(silhouette.height * .14 * spread))
            if shadow_width * shadow_height > 32_000_000:
                raise ValueError("阴影范围过大，请减小阴影扩散。")
            projected = silhouette.resize((shadow_width, shadow_height), Image.Resampling.LANCZOS)
            shadow_mask = Image.new("L", canvas.size)
            left = round(px + (box[0] + box[2]) / 2 - shadow_width / 2 + dx)
            top = round(py + box[3] - shadow_height * .65 + dy)
            shadow_mask.paste(projected, (left, top))
            shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(blur))
            shadow_mask = shadow_mask.point([round(x * opacity) for x in range(256)])
            shade = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shade.putalpha(shadow_mask)
            canvas.alpha_composite(shade)
        canvas.alpha_composite(product, (px, py))
        _save(canvas, output_path)
        return warnings

    def _text_regions(self, image):
        """Tile tall images; never squeeze an entire long detail page into OCR."""
        import numpy as np
        rgb = image.convert("RGB")
        w, h = rgb.size
        tile_height = min(2000, max(600, round(w * 1.6)))
        overlap = 100
        regions = []
        with self._lock:
            engine = self._ocr()
            for top in range(0, h, tile_height - overlap):
                tile = rgb.crop((0, top, w, min(h, top + tile_height)))
                ratio = min(1, 1400 / tile.width)
                if ratio < 1:
                    tile = tile.resize((1400, max(1, round(tile.height * ratio))), Image.Resampling.LANCZOS)
                # RapidOCR ndarray input follows cv2's BGR convention.
                results, _ = engine(np.asarray(tile)[:, :, ::-1].copy())
                for result in results or []:
                    polygon, text, confidence = result
                    if not text or float(confidence) < .45:
                        continue
                    points = [(max(0, min(w - 1, float(x) / ratio)), max(0, min(h - 1, float(y) / ratio + top))) for x, y in polygon]
                    x1 = max(0, int(min(v[0] for v in points)) - 2)
                    y1 = max(0, int(min(v[1] for v in points)) - 2)
                    x2 = min(w, math.ceil(max(v[0] for v in points)) + 3)
                    y2 = min(h, math.ceil(max(v[1] for v in points)) + 3)
                    if any(abs(old["box"][0] - x1) < 10 and abs(old["box"][1] - y1) < 12 for old in regions):
                        continue
                    regions.append({"box": (x1, y1, x2, y2), "text": str(text), "confidence": float(confidence)})
                if top + tile_height >= h:
                    break
        return sorted(regions, key=lambda r: (r["box"][1], r["box"][0]))

    def layers(self, input_path, output_dir, repair=False):
        image = _load(input_path)
        w, h = image.size
        if w * h > MAX_LAYER_PIXELS:
            raise ValueError("这张长图较大，请先按版块切分，再对每个版块自动分层。")
        output_dir = Path(output_dir)
        warnings = ["自动分层是主体与文字识别的推测；请核对范围。背景默认保留被遮挡处的透明空洞。"]
        text_regions = []
        if self.status()["engine"]["ocr"]:
            try:
                text_regions = self._text_regions(image)
                warnings.append("文字识别使用中英文模型；韩文及艺术字体可能漏识别或识别错误，请校对文字。")
            except Exception as exc:
                warnings.append("文字识别未成功：" + str(exc)[:180])
        else:
            warnings.append("本地 OCR 尚未就绪，当前只分离主体和背景。")
        max_text_layers = max(1, min(100, 250_000_000 // max(1, w * h)))
        if len(text_regions) > max_text_layers:
            warnings.append(f"文字区域较多，本次先输出前 {max_text_layers} 个；请先分段以处理其余文字。")
            text_regions = text_regions[:max_text_layers]
        text_mask = Image.new("L", image.size)
        text_draw = ImageDraw.Draw(text_mask)
        for region in text_regions:
            x1, y1, x2, y2 = region["box"]
            text_draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=255)
        product_mask, notes = self._mask(image)
        warnings.extend(notes)
        product_mask = ImageChops.multiply(product_mask, ImageOps.invert(text_mask))
        removed = ImageChops.lighter(product_mask, text_mask)
        background = image.copy()
        source_alpha = image.getchannel("A")
        if source_alpha.getextrema() == (255, 255):
            # Source-over: a partially transparent foreground over a formerly
            # opaque source still needs an opaque background at that pixel.
            background_alpha = removed.point([255] * 255 + [0])
        else:
            front = ImageChops.lighter(product_mask, ImageChops.multiply(text_mask, source_alpha))
            pairs = zip(source_alpha.tobytes(), front.tobytes())
            background_alpha = Image.frombytes("L", image.size, bytes(
                0 if a <= f else min(255, round((a - f) * 255 / (255 - f))) for a, f in pairs
            ))
        background.putalpha(background_alpha)
        if repair:
            if self._available("cv2") and self._available("numpy"):
                import cv2
                import numpy as np
                source = np.asarray(image.convert("RGB"))
                mask = np.asarray(removed.point(lambda v: 255 if v > 10 else 0))
                # Telea is local interpolation, not recovery of unseen scenery.
                filled = cv2.inpaint(source, mask, 5, cv2.INPAINT_TELEA)
                background = Image.fromarray(filled).convert("RGBA")
                warnings.append("背景修补由邻近像素推测，不是被遮挡背景的真实恢复；大面积空洞可能出现模糊或涂抹。")
            else:
                warnings.append("修补组件未就绪，保留背景透明空洞。")
        background_path = output_dir / "background.png"
        _save(background, background_path)
        layers = [{"name": "背景（修补推测）" if repair and background.getchannel("A").getextrema()[0] == 255 else "背景（遮挡处透明）", "type": "background", "path": str(background_path), "bounds": {"x": 0, "y": 0, "width": w, "height": h}}]
        product = image.copy()
        product.putalpha(product_mask)
        product_path = output_dir / "product.png"
        _save(product, product_path)
        layers.append({"name": "主体", "type": "product", "path": str(product_path), "bounds": _bounds(product_mask)})
        claimed_text = Image.new("L", image.size)
        claimed_draw = ImageDraw.Draw(claimed_text)
        for index, region in enumerate(text_regions, 1):
            x1, y1, x2, y2 = region["box"]
            # Original raster text area is preserved, not redrawn or flattened.
            layer = Image.new("RGBA", image.size)
            box = (x1, y1, x2, y2)
            crop = image.crop(box)
            crop.putalpha(ImageChops.multiply(crop.getchannel("A"), ImageOps.invert(claimed_text.crop(box))))
            layer.paste(crop, (x1, y1))
            claimed_draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=255)
            dest = output_dir / f"text-{index:03d}.png"
            _save(layer, dest)
            layers.append({"name": f"文字 {index}", "type": "text", "path": str(dest), "bounds": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}, "text": region["text"], "confidence": region["confidence"], "editableTextIsApproximation": True})
        return {"layers": layers, "warnings": warnings, "width": w, "height": h}
