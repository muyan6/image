"""Small-host controls and basic CI operations, not advanced compression or AI."""
import functools
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

class QueueFull(RuntimeError):pass

class BoundedExecutor(ThreadPoolExecutor):
    def __init__(self,workers,queued):
        super().__init__(max_workers=workers,thread_name_prefix='rescue')
        self.workers=workers;self.capacity=workers+queued
        self._slots=threading.BoundedSemaphore(self.capacity)
        self._counts=threading.Lock();self._active=0;self._accepted=0
    def submit(self,fn,*args,**kwargs):
        if not self._slots.acquire(blocking=False):raise QueueFull('任务队列已满，请稍后再试')
        with self._counts:self._accepted+=1
        def run():
            with self._counts:self._active+=1
            try:return fn(*args,**kwargs)
            finally:
                with self._counts:self._active-=1
        def release(f):
            with self._counts:self._accepted-=1
            self._slots.release()
        try:
            future=super().submit(run);future.add_done_callback(release);return future
        except Exception:
            with self._counts:self._accepted-=1
            self._slots.release();raise
    def snapshot(self):
        with self._counts:return {'workers':self.workers,'active':self._active,'queued':self._accepted-self._active,'capacity':self.capacity,'local_image_parallelism':1}

IMAGE_LOCK=threading.RLock()
UPLOAD_SLOTS=threading.BoundedSemaphore(2)

def upload_limited(fn, should_limit=None):
    @functools.wraps(fn)
    def run(*args,**kwargs):
        if should_limit is not None and not should_limit():return fn(*args,**kwargs)
        with UPLOAD_SLOTS:return fn(*args,**kwargs)
    run.__signature__=inspect.signature(fn,eval_str=True)
    return run


def image_limited(fn):
    @functools.wraps(fn)
    def run(*args,**kwargs):
        with IMAGE_LOCK:return fn(*args,**kwargs)
    return run


def normalization_rule(width, height, long_side, aspect=''):
    parts=['imageMogr2','auto-orient']
    if aspect not in ('','original','auto','none'):
        try:
            a,b=map(float,aspect.split(':'));ratio=a/b
            if a<=0 or b<=0:raise ValueError()
        except (ValueError,ZeroDivisionError):raise ValueError('Invalid aspect ratio')
        crop_w,crop_h=width,height
        if width/height>ratio:crop_w=max(1,int(height*ratio))
        else:crop_h=max(1,int(width/ratio))
        parts+=['gravity','center','crop',f'{crop_w}x{crop_h}']
    if long_side:parts+=['thumbnail',f'{long_side}x{long_side}>']
    return '/'.join(parts+['format','jpg','quality','85','strip'])
