import sys
import os
import re
import subprocess
import threading
import time
import json 
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QPushButton, QSlider, QFileDialog, QColorDialog, 
                             QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, 
                             QScrollArea, QTabWidget, QFormLayout, QProgressBar, QMessageBox, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QImage, QColor, QIcon

# -----------------------------------------------------------------------------
# 核心逻辑：LRC解析与图像渲染
# -----------------------------------------------------------------------------

# --- 在 main.py 顶部 import 区域之后添加这个函数 ---
def get_ffmpeg_path():
    """获取 ffmpeg 的绝对路径，兼容开发环境和打包后的环境"""
    if getattr(sys, 'frozen', False):
        # 如果是打包后的环境 (PyInstaller)
        base_path = sys._MEIPASS
        print("frozened")
    else:
        # 如果是开发环境
        base_path = os.path.dirname(os.path.abspath(__file__))
        print(base_path)
    # 拼接 ffmpeg.exe 的路径
    ffmpeg_exe = os.path.join(base_path, 'ffmpeg.exe')
    print(ffmpeg_exe)
    # 如果找不到（比如在 onedir 模式下可能在根目录），回退到默认命令
    if not os.path.exists(ffmpeg_exe):
        # 尝试在当前工作目录找
        if os.path.exists('ffmpeg.exe'):
            return 'ffmpeg.exe'
        return 'ffmpeg' # 最后的希望：系统环境变量
    print(ffmpeg_exe)
    return ffmpeg_exe

class LrcParser:
    @staticmethod
    def parse(file_path):
        lyrics = []
        if not file_path or not os.path.exists(file_path):
            return lyrics
        
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            
        time_pattern = re.compile(r'\[(\d{2}):(\d{2})\.(\d{2,3})\]')
        
        for line in lines:
            line = line.strip()
            match = time_pattern.search(line)
            if match:
                mm, ss, ms = match.groups()
                # 统一转为秒
                ms_val = int(ms)
                if len(ms) == 2: ms_val *= 10
                
                total_seconds = int(mm) * 60 + int(ss) + ms_val / 1000.0
                text = time_pattern.sub('', line).strip()
                if text:
                    lyrics.append({'time': total_seconds, 'text': text})
        
        # 排序
        lyrics.sort(key=lambda x: x['time'])
        return lyrics

class FrameRenderer:
    """
    负责绘制每一帧图像的核心类
    """
    def __init__(self, params, lyrics):
        self.p = params
        self.lyrics = lyrics
        # 预加载字体以提高性能
        try:
            self.lyric_font = ImageFont.truetype(self.p['font_path'], self.p['font_size'])
            self.meta_title_font = ImageFont.truetype(self.p['font_path'], int(self.p['font_size'] * 1.5))
            self.meta_info_font = ImageFont.truetype(self.p['font_path'], int(self.p['font_size'] * 0.8))
        except:
            self.lyric_font = ImageFont.load_default()
            self.meta_title_font = ImageFont.load_default()
            self.meta_info_font = ImageFont.load_default()

    def get_current_line_index(self, current_time):
        idx = -1
        for i, line in enumerate(self.lyrics):
            if current_time >= line['time']:
                idx = i
            else:
                break
        return idx

    def draw_text_with_effects(self, draw, text, x, y, font, color, alpha, scale, blur_radius, align, shadow, stroke):
        """绘制带有各种特效的单行文本"""
        if alpha <= 5: return None, 0, 0 #太淡了不画
        
        # 颜色处理
        r, g, b = color
        fill_color = (r, g, b, int(alpha))
        
        # 如果需要缩放或模糊，建议先在临时图层绘制再贴回去，或者直接计算坐标
        # 为了性能和效果平衡，这里主要处理位置和Alpha，模糊和缩放通过PIL Image操作
        
        # 计算文字大小
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        
        # 创建临时画布用于处理单行特效（缩放/模糊）
        # 增加padding防止模糊被切
        padding = 20
        temp_w, temp_h = int(w + padding*2), int(h + padding*2)
        if temp_w <= 0 or temp_h <= 0: return

        txt_img = Image.new('RGBA', (temp_w, temp_h), (0,0,0,0))
        txt_draw = ImageDraw.Draw(txt_img)
        
        # 本地坐标
        lx, ly = padding, padding
        
        # 1. 绘制阴影
        if shadow['enabled']:
            sr, sg, sb = shadow['color']
            s_alpha = int(alpha * 0.6) # 阴影透明度随主透明度降低
            s_fill = (sr, sg, sb, s_alpha)
            txt_draw.text((lx + shadow['x'], ly + shadow['y']), text, font=font, fill=s_fill)

        # 2. 绘制描边
        stroke_width = stroke['width'] if stroke['enabled'] else 0
        stroke_fill = stroke['color'] + (int(alpha),) if stroke['enabled'] else None

        # 3. 绘制主体
        txt_draw.text((lx, ly), text, font=font, fill=fill_color, 
                      stroke_width=stroke_width, stroke_fill=stroke_fill)

        # 4. 处理缩放 (Scale)
        if scale != 1.0:
            new_w = int(temp_w * scale)
            new_h = int(temp_h * scale)
            if new_w > 0 and new_h > 0:
                txt_img = txt_img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
        
        # 5. 处理模糊 (Feather/Blur)
        if blur_radius > 0:
            txt_img = txt_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

        # 6. 计算最终粘贴位置
        # 对齐方式修正X
        final_w, final_h = txt_img.size
        dest_x = x
        if align == 'center':
            dest_x = x - final_w // 2
        elif align == 'right':
            dest_x = x - final_w
        
        dest_y = y - final_h // 2 # 垂直居中绘制

        return txt_img, dest_x, dest_y

    def render(self, current_time):
        width = self.p['width']
        height = self.p['height']
        
        # 创建全透明背景
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # 1. 绘制顶部信息 (Title, Artist, Album)
        margin_top = 40
        header_x = width // 2 if self.p['align'] == 'center' else (50 if self.p['align'] == 'left' else width - 50)
        
        # Title
        title_w = draw.textlength(self.p['meta_title'], font=self.meta_title_font)
        tx = header_x - title_w//2 if self.p['align'] == 'center' else (header_x if self.p['align'] == 'left' else header_x - title_w)
        draw.text((tx, margin_top), self.p['meta_title'], font=self.meta_title_font, fill=self.p['font_color']+(255,))
        
        # Info
        info_text = f"{self.p['meta_artist']} - {self.p['meta_album']}"
        info_w = draw.textlength(info_text, font=self.meta_info_font)
        ix = header_x - info_w//2 if self.p['align'] == 'center' else (header_x if self.p['align'] == 'left' else header_x - info_w)
        draw.text((ix, margin_top + self.p['font_size']*2), info_text, font=self.meta_info_font, fill=self.p['font_color']+(200,))

        # 2. 歌词滚动区域计算
        scroll_area_top = margin_top + self.p['font_size'] * 4
        scroll_area_height = height - scroll_area_top - 50
        center_y = scroll_area_top + scroll_area_height // 2
        
        # 找到当前行
        curr_idx = self.get_current_line_index(current_time)
        
        # 平滑滚动计算 (Optional: Calculate offset based on time progress within line)
        # 简单模式：当前行永远在C位，不随时间微移，直接居中
        # 复杂模式：如果在两行之间，可以做插值。为了模仿Apple Music，当前行高亮且居中，切换时有动画。
        # 这里简化：当前行绝对居中。
        
        # 绘制歌词
        # 向上和向下遍历
        visible_lines = self.p['visible_lines']
        line_spacing = self.p['line_spacing']
        
        # 渲染列表：包含 (index, distance_level)
        render_list = []
        render_list.append((curr_idx, 0)) # 中心行
        
        for i in range(1, visible_lines // 2 + 2):
            if curr_idx - i >= 0: render_list.append((curr_idx - i, -i)) # 上方
            if curr_idx + i < len(self.lyrics): render_list.append((curr_idx + i, i)) # 下方

        for idx, dist_level in render_list:
            if idx < 0 or idx >= len(self.lyrics): continue
            
            line_text = self.lyrics[idx]['text']
            abs_dist = abs(dist_level)
            
            # 计算参数
            # 系数递增：base + (coeff * abs_dist)
            scale = max(0.1, 1.0 - (self.p['scale_decay'] * abs_dist))
            
            # 透明度
            alpha = max(0, 255 - (self.p['fade_decay'] * abs_dist * 50)) 
            if dist_level == 0: alpha = 255 # 当前行完全不透明
            
            # 羽化/模糊
            blur = self.p['blur_base'] + (self.p['blur_inc'] * abs_dist)
            if dist_level == 0: blur = 0 # 当前行清晰
            
            # Y坐标计算
            y_pos = center_y + (dist_level * (self.p['font_size'] + line_spacing))
            
            # 绘制
            res_img, dx, dy = self.draw_text_with_effects(
                draw, line_text, header_x, y_pos, 
                self.lyric_font, self.p['font_color'], alpha, scale, blur, 
                self.p['align'], self.p['shadow'], self.p['stroke']
            )
            
            if res_img:
                img.paste(res_img, (int(dx), int(dy)), res_img)

        return img

# -----------------------------------------------------------------------------
# 导出线程
# -----------------------------------------------------------------------------
class ExportThread(QThread):
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, params, lyrics, output_path):
        super().__init__()
        self.params = params
        self.lyrics = lyrics
        self.output_path = output_path
        self.is_running = True

    def run(self):
        try:
            width = self.params['width']
            height = self.params['height']
            fps = 30
            duration = self.params['duration']
            total_frames = int(duration * fps)

            # FFmpeg 命令: ProRes 4444 (ap4h) 支持 Alpha 通道
            # -pix_fmt yuva444p10le 是关键
            ffmpeg_path = get_ffmpeg_path() 
            cmd = [
                ffmpeg_path,
                '-y', # 覆盖输出
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-s', f'{width}x{height}',
                '-pix_fmt', 'rgba',
                '-r', str(fps),
                '-i', '-', # 从管道输入
                '-c:v', 'prores_ks', 
                '-profile:v', '4444', # ProRes 4444 for Alpha
                '-pix_fmt', 'yuva444p10le', # 10bit alpha
                '-b:v', self.params['bitrate'],
                self.output_path
            ]
            
            # 如果在Windows上不显示CMD窗口
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            process = subprocess.Popen(
                cmd, 
                stdin=subprocess.PIPE, 
                stderr=subprocess.DEVNULL, # <--- 关键修改：改为 DEVNULL
                stdout=subprocess.DEVNULL, # <--- 建议：把 stdout 也丢弃
                startupinfo=startupinfo
            )

            renderer = FrameRenderer(self.params, self.lyrics)

            for i in range(total_frames):
                if not self.is_running:
                    process.stdin.close()
                    process.wait()
                    return

                t = i / fps
                img = renderer.render(t)
                
                # 转换为bytes
                process.stdin.write(img.tobytes())
                
                # 更新进度
                if i % 10 == 0:
                    self.progress_signal.emit(int((i / total_frames) * 100))

            process.stdin.close()
            process.wait()
            
            if process.returncode != 0:
                # 因为没有捕获 stderr，只能提示通用错误
                self.error_signal.emit("FFmpeg Error: Export failed (Unknown error).")
            else:
                self.finished_signal.emit("Export Complete!")

        except Exception as e:
            self.error_signal.emit(str(e))

    def stop(self):
        self.is_running = False

# -----------------------------------------------------------------------------
# GUI 界面
# -----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProRes Transparent Lyric Video Generator")
        self.resize(1280, 800)

        # 默认参数
        self.params = {
            'lrc_path': '',
            'width': 1920,
            'height': 1080,
            'duration': 60,
            'bitrate': '50M',
            'visible_lines': 10,
            'line_spacing': 80,
            'align': 'center', # left, center, right
            'font_path': 'arial.ttf', # 默认简单字体，用户需选择
            'font_size': 60,
            'font_color': (255, 255, 255),
            'scale_decay': 0.1,
            'fade_decay': 0.5,
            'blur_base': 0,
            'blur_inc': 2,
            'meta_title': 'Song Title',
            'meta_artist': 'Artist',
            'meta_album': 'Album',
            'shadow': {'enabled': False, 'color': (0,0,0), 'x': 2, 'y': 2},
            'stroke': {'enabled': False, 'color': (0,0,0), 'width': 2}
        }
        self.lyrics = []
        self.renderer = None

        self.init_ui()

    def save_presets(self):
        """保存当前所有设置到JSON文件"""
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Settings", "", "JSON Files (*.json)")
        if not file_path:
            return

        # 获取当前UI的所有参数
        current_settings = self.get_ui_params()
        
        # 补充一些 get_ui_params 可能没包含但需要的路径信息
        current_settings['lrc_path'] = self.params.get('lrc_path', '')
        current_settings['font_path'] = self.params.get('font_path', '')
        current_settings['font_color'] = self.params.get('font_color', (255, 255, 255))

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(current_settings, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "Success", "Settings saved successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{str(e)}")

    def load_presets(self):
        """从JSON文件加载设置并更新UI"""
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Settings", "", "JSON Files (*.json)")
        if not file_path or not os.path.exists(file_path):
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.apply_settings_to_ui(data)
            self.schedule_preview() # 刷新预览
            QMessageBox.information(self, "Success", "Settings loaded successfully!")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load settings:\n{str(e)}")

    def apply_settings_to_ui(self, data):
        """将字典数据应用到界面控件"""
        # 1. 更新内部 params (作为后备)
        self.params.update(data)

        # ============================================================
        # [关键修复] 强制将颜色列表转回元组
        # JSON加载会将 (r,g,b) 变成 [r,g,b]，会导致后续渲染计算时报错
        # ============================================================
        if 'font_color' in self.params:
            self.params['font_color'] = tuple(self.params['font_color'])

        if 'shadow' in self.params and 'color' in self.params['shadow']:
            self.params['shadow']['color'] = tuple(self.params['shadow']['color'])
            
        if 'stroke' in self.params and 'color' in self.params['stroke']:
            self.params['stroke']['color'] = tuple(self.params['stroke']['color'])
        # ============================================================

        # 2. 基础信息
        if 'lrc_path' in data and data['lrc_path']:
            # 尝试重新加载 LRC
            if os.path.exists(data['lrc_path']):
                self.params['lrc_path'] = data['lrc_path']
                self.btn_lrc.setText(os.path.basename(data['lrc_path']))
                self.lyrics = LrcParser.parse(data['lrc_path'])
            else:
                self.btn_lrc.setText("File not found")

        self.inp_title.setText(data.get('meta_title', ''))
        self.inp_artist.setText(data.get('meta_artist', ''))
        self.inp_album.setText(data.get('meta_album', ''))

        # 3. 视频设置
        self.spin_w.setValue(data.get('width', 1920))
        self.spin_h.setValue(data.get('height', 1080))
        self.spin_dur.setValue(data.get('duration', 60))
        
        bitrate = data.get('bitrate', '50M')
        idx = self.inp_bitrate.findText(bitrate)
        if idx >= 0: self.inp_bitrate.setCurrentIndex(idx)

        # 4. 字体与样式
        if 'font_path' in data and data['font_path']:
            self.params['font_path'] = data['font_path']
            self.btn_font.setText(os.path.basename(data['font_path']))
        
        self.spin_fsize.setValue(data.get('font_size', 60))
        
        if 'font_color' in data:
            c = data['font_color'] 
            # 确保 UI 按钮颜色也更新
            self.btn_color.setStyleSheet(f"background-color: rgb({c[0]}, {c[1]}, {c[2]})")

        align = data.get('align', 'center')
        idx_align = self.combo_align.findText(align)
        if idx_align >= 0: self.combo_align.setCurrentIndex(idx_align)

        # 5. 滚动特效
        self.spin_lines.setValue(data.get('visible_lines', 10))
        self.spin_spacing.setValue(data.get('line_spacing', 80))
        self.spin_scale_dec.setValue(data.get('scale_decay', 0.1))
        self.spin_fade_dec.setValue(data.get('fade_decay', 0.5))
        self.spin_blur_inc.setValue(data.get('blur_inc', 2.0))

        # 6. 装饰 (Nested dicts)
        shadow_data = data.get('shadow', {})
        self.chk_shadow.setChecked(shadow_data.get('enabled', False))
        self.spin_shadow_off.setValue(shadow_data.get('x', 2)) 

        stroke_data = data.get('stroke', {})
        self.chk_stroke.setChecked(stroke_data.get('enabled', False))
        self.spin_stroke_w.setValue(stroke_data.get('width', 2))

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)

        # --- 左侧控制面板 ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        form_layout = QFormLayout(scroll_content)  # <--- 1. 这里先定义 form_layout
        
        # ==========================================
        # [新增] 顶部配置管理按钮
        # ==========================================
        preset_layout = QHBoxLayout()
        btn_save_preset = QPushButton("Save Settings")
        # 移除了QIcon.fromTheme，因为在Windows上可能报错或不显示，或者你可以保留如果你的环境支持
        btn_save_preset.clicked.connect(self.save_presets)
        
        btn_load_preset = QPushButton("Load Settings")
        btn_load_preset.clicked.connect(self.load_presets)
        
        preset_layout.addWidget(btn_save_preset)
        preset_layout.addWidget(btn_load_preset)
        form_layout.addRow(preset_layout)          # <--- 2. 然后再使用它
        # ==========================================

        # 1. 文件与基础信息
        group_basic = QGroupBox("Basic Info")
        gl_basic = QFormLayout()
        
        self.btn_lrc = QPushButton("Select LRC File")
        self.btn_lrc.clicked.connect(self.load_lrc)
        gl_basic.addRow("LRC File:", self.btn_lrc)
        
        self.inp_title = QLineEdit(self.params['meta_title'])
        self.inp_artist = QLineEdit(self.params['meta_artist'])
        self.inp_album = QLineEdit(self.params['meta_album'])
        gl_basic.addRow("Title:", self.inp_title)
        gl_basic.addRow("Artist:", self.inp_artist)
        gl_basic.addRow("Album:", self.inp_album)
        group_basic.setLayout(gl_basic)
        form_layout.addRow(group_basic)

        # 2. 视频设置
        group_video = QGroupBox("Video Settings")
        gl_video = QFormLayout()
        self.spin_w = QSpinBox(); self.spin_w.setRange(100, 7680); self.spin_w.setValue(1920)
        self.spin_h = QSpinBox(); self.spin_h.setRange(100, 4320); self.spin_h.setValue(1080)
        self.spin_dur = QSpinBox(); self.spin_dur.setRange(1, 3600); self.spin_dur.setValue(60)
        self.inp_bitrate = QComboBox(); self.inp_bitrate.addItems(["20M", "50M", "100M", "200M"])
        self.inp_bitrate.setCurrentText("50M")
        
        gl_video.addRow("Width:", self.spin_w)
        gl_video.addRow("Height:", self.spin_h)
        gl_video.addRow("Duration (s):", self.spin_dur)
        gl_video.addRow("Bitrate:", self.inp_bitrate)
        group_video.setLayout(gl_video)
        form_layout.addRow(group_video)

        # 3. 字体与样式
        group_style = QGroupBox("Style & Font")
        gl_style = QFormLayout()
        
        self.btn_font = QPushButton("Select Font File (.ttf)")
        self.btn_font.clicked.connect(self.select_font)
        self.spin_fsize = QSpinBox(); self.spin_fsize.setValue(60)
        self.btn_color = QPushButton("Color"); self.btn_color.setStyleSheet("background-color: white")
        self.btn_color.clicked.connect(self.select_color)
        self.combo_align = QComboBox(); self.combo_align.addItems(['left', 'center', 'right']); self.combo_align.setCurrentText('center')
        
        gl_style.addRow("Font:", self.btn_font)
        gl_style.addRow("Size:", self.spin_fsize)
        gl_style.addRow("Color:", self.btn_color)
        gl_style.addRow("Align:", self.combo_align)
        group_style.setLayout(gl_style)
        form_layout.addRow(group_style)
        
        # 4. 滚动特效
        group_fx = QGroupBox("Scroll Effects")
        gl_fx = QFormLayout()
        
        self.spin_lines = QSpinBox(); self.spin_lines.setValue(10)
        self.spin_spacing = QSpinBox(); self.spin_spacing.setRange(0, 500); self.spin_spacing.setValue(80)
        self.spin_scale_dec = QDoubleSpinBox(); self.spin_scale_dec.setSingleStep(0.01); self.spin_scale_dec.setValue(0.1)
        self.spin_fade_dec = QDoubleSpinBox(); self.spin_fade_dec.setSingleStep(0.1); self.spin_fade_dec.setValue(0.5)
        self.spin_blur_inc = QDoubleSpinBox(); self.spin_blur_inc.setValue(2.0)
        
        gl_fx.addRow("Visible Lines:", self.spin_lines)
        gl_fx.addRow("Line Spacing:", self.spin_spacing)
        gl_fx.addRow("Scale Decay:", self.spin_scale_dec)
        gl_fx.addRow("Fade Coeff:", self.spin_fade_dec)
        gl_fx.addRow("Blur Inc:", self.spin_blur_inc)
        group_fx.setLayout(gl_fx)
        form_layout.addRow(group_fx)

        # 5. 装饰 (阴影/描边)
        group_dec = QGroupBox("Decorations")
        gl_dec = QFormLayout()
        
        self.chk_shadow = QCheckBox("Enable Shadow")
        self.spin_shadow_off = QSpinBox(); self.spin_shadow_off.setValue(2)
        
        self.chk_stroke = QCheckBox("Enable Stroke")
        self.spin_stroke_w = QSpinBox(); self.spin_stroke_w.setValue(2)
        
        gl_dec.addRow(self.chk_shadow)
        gl_dec.addRow("Shadow Offset:", self.spin_shadow_off)
        gl_dec.addRow(self.chk_stroke)
        gl_dec.addRow("Stroke Width:", self.spin_stroke_w)
        group_dec.setLayout(gl_dec)
        form_layout.addRow(group_dec)

        scroll.setWidget(scroll_content)
        scroll.setFixedWidth(400)
        layout.addWidget(scroll)

        # --- 右侧预览与导出 ---
        right_layout = QVBoxLayout()
        
        # 预览区域 (黑色背景以查看透明内容，或格子背景)
        self.preview_label = QLabel("Preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background-color: #333; border: 1px solid #555;")
        self.preview_label.setMinimumSize(640, 360)
        right_layout.addWidget(self.preview_label)
        
        # 进度条控制
        control_layout = QHBoxLayout()
        self.slider_time = QSlider(Qt.Orientation.Horizontal)
        self.slider_time.setRange(0, 6000) # 0 to 60.00s
        self.slider_time.valueChanged.connect(self.update_preview)
        self.lbl_time = QLabel("00:00")
        
        control_layout.addWidget(QLabel("Time:"))
        control_layout.addWidget(self.slider_time)
        control_layout.addWidget(self.lbl_time)
        right_layout.addLayout(control_layout)
        
        # 导出按钮
        self.btn_export = QPushButton("EXPORT MOV (Alpha)")
        self.btn_export.setMinimumHeight(50)
        self.btn_export.setStyleSheet("font-size: 16px; font-weight: bold; background-color: #007AFF; color: white;")
        self.btn_export.clicked.connect(self.start_export)
        right_layout.addWidget(self.btn_export)
        
        self.progress_bar = QProgressBar()
        right_layout.addWidget(self.progress_bar)
        
        layout.addLayout(right_layout)

        # 绑定信号更新预览
        inputs = [self.inp_title, self.inp_artist, self.inp_album, self.spin_w, self.spin_h, 
                  self.spin_fsize, self.spin_lines, self.spin_spacing, self.spin_scale_dec, 
                  self.spin_fade_dec, self.spin_blur_inc, self.chk_shadow, self.chk_stroke,
                  self.spin_shadow_off, self.spin_stroke_w, self.combo_align]
        
        for i in inputs:
            if isinstance(i, (QLineEdit, QSpinBox, QDoubleSpinBox)):
                if isinstance(i, QLineEdit): i.textChanged.connect(self.schedule_preview)
                else: i.valueChanged.connect(self.schedule_preview)
            elif isinstance(i, QCheckBox):
                i.stateChanged.connect(self.schedule_preview)
            elif isinstance(i, QComboBox):
                i.currentIndexChanged.connect(self.schedule_preview)

        # 使用Timer防止频繁刷新卡顿
        self.preview_timer = QTimer()
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.update_preview)

    def load_lrc(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select LRC", "", "LRC Files (*.lrc)")
        if path:
            self.params['lrc_path'] = path
            self.btn_lrc.setText(os.path.basename(path))
            self.lyrics = LrcParser.parse(path)
            if self.lyrics:
                duration = int(self.lyrics[-1]['time']) + 5
                self.spin_dur.setValue(duration)
                self.slider_time.setRange(0, duration * 100)
            self.schedule_preview()

    def select_font(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Font", "", "Font Files (*.ttf *.otf)")
        if path:
            self.params['font_path'] = path
            self.btn_font.setText(os.path.basename(path))
            self.schedule_preview()

    def select_color(self):
        c = QColorDialog.getColor()
        if c.isValid():
            self.params['font_color'] = (c.red(), c.green(), c.blue())
            self.btn_color.setStyleSheet(f"background-color: {c.name()}")
            self.schedule_preview()

    def schedule_preview(self):
        self.preview_timer.start(200) # 200ms debounce

    def get_ui_params(self):
        p = self.params.copy()
        p['meta_title'] = self.inp_title.text()
        p['meta_artist'] = self.inp_artist.text()
        p['meta_album'] = self.inp_album.text()
        p['width'] = self.spin_w.value()
        p['height'] = self.spin_h.value()
        p['duration'] = self.spin_dur.value()
        p['bitrate'] = self.inp_bitrate.currentText()
        p['font_size'] = self.spin_fsize.value()
        p['visible_lines'] = self.spin_lines.value()
        p['line_spacing'] = self.spin_spacing.value()
        p['scale_decay'] = self.spin_scale_dec.value()
        p['fade_decay'] = self.spin_fade_dec.value()
        p['blur_inc'] = self.spin_blur_inc.value()
        p['align'] = self.combo_align.currentText()
        p['shadow']['enabled'] = self.chk_shadow.isChecked()
        p['shadow']['x'] = self.spin_shadow_off.value()
        p['shadow']['y'] = self.spin_shadow_off.value()
        p['stroke']['enabled'] = self.chk_stroke.isChecked()
        p['stroke']['width'] = self.spin_stroke_w.value()
        return p

    def update_preview(self):
        if not self.lyrics:
            return

        current_time = self.slider_time.value() / 100.0
        self.lbl_time.setText(f"{int(current_time//60):02d}:{int(current_time%60):02d}")
        
        current_params = self.get_ui_params()
        # 预览降低分辨率以提高速度
        preview_scale = 0.5
        current_params['width'] = int(current_params['width'] * preview_scale)
        current_params['height'] = int(current_params['height'] * preview_scale)
        current_params['font_size'] = int(current_params['font_size'] * preview_scale)
        current_params['line_spacing'] = int(current_params['line_spacing'] * preview_scale)
        
        renderer = FrameRenderer(current_params, self.lyrics)
        pil_image = renderer.render(current_time)
        
        # PIL to QPixmap
        im_data = pil_image.convert("RGBA").tobytes("raw", "RGBA")
        qim = QImage(im_data, pil_image.width, pil_image.height, QImage.Format.Format_RGBA8888)
        pix = QPixmap.fromImage(qim)
        
        # Fit to label
        self.preview_label.setPixmap(pix.scaled(self.preview_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def start_export(self):
        if not self.lyrics:
            QMessageBox.warning(self, "Error", "Please load an LRC file first.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save Video", "", "MOV Video (*.mov)")
        if not path:
            return
        
        self.btn_export.setEnabled(False)
        self.progress_bar.setValue(0)
        
        self.thread = ExportThread(self.get_ui_params(), self.lyrics, path)
        self.thread.progress_signal.connect(self.progress_bar.setValue)
        self.thread.finished_signal.connect(self.export_finished)
        self.thread.error_signal.connect(self.export_error)
        self.thread.start()

    def export_finished(self, msg):
        self.btn_export.setEnabled(True)
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "Success", msg)

    def export_error(self, msg):
        self.btn_export.setEnabled(True)
        QMessageBox.critical(self, "Error", msg)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # 设置深色主题风格
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
