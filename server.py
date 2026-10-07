#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase-1 演示后端:单进程同时提供静态 React 页 + 处理 API。
  GET  /            -> index.html(React SPA,React 经 CDN 加载)
  POST /api/process -> {image_b64, size, bead} -> {pdf_b64, grid_b64, bom}

上传图片以 base64 JSON 提交(避免 multipart 解析);返回 PDF + 像素网格 + BOM。
仅用 Python 标准库,无需安装 Flask/FastAPI。
"""
import base64, io, json, os, tempfile, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

import bead_pipeline as bp

PORT = int(os.environ.get('PORT', '8000'))
PALETTE = os.path.join(HERE, 'palettes', 'fuse_beads_starter.csv')

# 演示配置:定价与阶梯(色卡由 palettes/*.csv 配置,可直接改此处)
SIZES = [16, 32, 50]
BEADS = [{'v': 'circle', 'l': '圆豆'}, {'v': 'square', 'l': '方豆'}]
PRICING = {16: ('256 颗', 9.9), 32: ('1,024 颗', 19.9), 50: ('2,500 颗', 39.9)}


def process_image(image_b64, size, bead):
    palette = bp.load_palette(PALETTE)
    data = base64.b64decode(image_b64)
    img_path = tempfile.mktemp(suffix='.png')
    with open(img_path, 'wb') as f:
        f.write(data)
    try:
        grid = bp.run_pipeline(img_path, palette, size=size, k=24, min_count=5)

        grid_buf = io.BytesIO()
        bp.render_grid_png(grid, palette, size, size, grid_buf, bead=bead)
        grid_b64 = base64.b64encode(grid_buf.getvalue()).decode()

        pdf_path = tempfile.mktemp(suffix='.pdf')
        bp.export_pdf(grid, palette, size, size, pdf_path)
        with open(pdf_path, 'rb') as f:
            pdf_b64 = base64.b64encode(f.read()).decode()
        os.unlink(pdf_path)
    finally:
        os.unlink(img_path)

    bom = bp.build_bom(grid, palette)
    return pdf_b64, grid_b64, bom



# Phase-2: 异步队列(标准库线程池,可无缝替换为 RabbitMQ 消费者)
JOB_LOCK = threading.Lock()
JOBS = {}
WORKER = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bead-worker')


def _run_job(job_id, image_b64, size, bead):
    try:
        JOBS[job_id]['status'] = 'running'
        pdf_b64, grid_b64, bom = process_image(image_b64, size, bead)
        JOBS[job_id].update({
            'status': 'done', 'progress': 100,
            'pdf_b64': pdf_b64, 'grid_b64': grid_b64,
            'bom': [{'id': x['id'], 'name': x['name'],
                     'hex': x['hex'], 'count': c} for x, c in bom],
        })
    except Exception as e:
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)


def submit_job(image_b64, size, bead):
    job_id = uuid.uuid4().hex[:12]
    rec = {'job_id': job_id, 'status': 'queued', 'progress': 0,
           'size': size, 'bead': bead, 'created': time.time()}
    with JOB_LOCK:
        JOBS[job_id] = rec
    WORKER.submit(_run_job, job_id, image_b64, size, bead)
    return job_id


def get_job(job_id):
    with JOB_LOCK:
        rec = JOBS.get(job_id)
        return dict(rec) if rec else None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                self._send(200, 'text/html; charset=utf-8', f.read())
        elif path == '/api/config':
            cfg = {
                'sizes': SIZES,
                'beads': BEADS,
                'pricing': {str(s): {'limit': lim, 'price': pr}
                            for s, (lim, pr) in PRICING.items()},
                'palette_count': len(bp.load_palette(PALETTE)),
            }
            self._send(200, 'application/json',
                       json.dumps(cfg, ensure_ascii=False))
        elif path.startswith('/api/job/'):
            job = get_job(path.rsplit('/', 1)[1])
            if job is None:
                self._send(404, 'application/json',
                           json.dumps({'error': 'no such job'},
                                    ensure_ascii=False))
            else:
                self._send(200, 'application/json',
                           json.dumps(job, ensure_ascii=False))
        else:
            self._send(404, 'text/plain', b'not found')

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        if path == '/api/process':
            try:
                req = json.loads(raw.decode('utf-8'))
                img = req['image_b64']
                size = int(req.get('size', 32))
                bead = req.get('bead', 'circle')
                pdf_b64, grid_b64, bom = process_image(img, size, bead)
                payload = {
                    'pdf_b64': pdf_b64,
                    'grid_b64': grid_b64,
                    'bom': [{'id': x['id'], 'name': x['name'],
                             'hex': x['hex'], 'count': c} for x, c in bom],
                }
                self._send(200, 'application/json',
                           json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                self._send(400, 'application/json',
                           json.dumps({'error': str(e)}, ensure_ascii=False))
        elif path == '/api/submit':
            req = json.loads(raw.decode('utf-8'))
            job_id = submit_job(req['image_b64'],
                                int(req.get('size', 32)),
                                req.get('bead', 'circle'))
            self._send(202, 'application/json',
                       json.dumps({'job_id': job_id,
                                   'status': 'queued'},
                                ensure_ascii=False))
        elif path == '/api/batch':
            req = json.loads(raw.decode('utf-8'))
            items = req.get('images', [])
            ids = [submit_job(it['image_b64'],
                               int(it.get('size', 32)),
                               it.get('bead', 'circle'))
                   for it in items]
            self._send(202, 'application/json',
                       json.dumps({'job_ids': ids},
                                ensure_ascii=False))
        else:
            self._send(404, 'application/json',
                       json.dumps({'error': 'not found'}, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass  # 保持控制台安静


def main():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'流水线拼豆 Phase-1 演示服务: http://localhost:{PORT}')
    print('按 Ctrl+C 退出')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()