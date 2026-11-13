"""Keep the QQ Music app MQTT subscription alive throughout a QR login."""
import asyncio
import base64
import contextlib
import threading
import time


class MobileQR:
    def __init__(self, on_result, lifetime=180):
        self.on_result=on_result
        self.lifetime=lifetime
        self.ready=threading.Event()
        self.cancelled=threading.Event()
        self.loop=None
        self.task=None
        self.qr=None
        self.error=False
        self.thread=threading.Thread(target=self.run,daemon=True,name='qq-mobile-login')
        self.thread.start()

    def cancel(self):
        if self.cancelled.is_set(): return
        self.cancelled.set()
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)

    def image(self):
        if not self.ready.wait(30) or self.error or not self.qr:
            self.cancel()
            raise ValueError('QQ 音乐扫码服务暂不可用')
        return self.qr

    def run(self):
        try:
            asyncio.run(self.watch())
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            self.error=True
            self.on_result('expired',None)
        except Exception:
            self.error=True
            self.on_result('failed',None)
        finally:
            self.ready.set()

    async def watch(self):
        import anyio
        import hub_qq
        from qqmusic_api.models.login import QRLoginType
        self.loop=asyncio.get_running_loop()
        self.task=asyncio.current_task()
        if self.cancelled.is_set(): return
        async with asyncio.timeout(self.lifetime):
            async with hub_qq.client() as client:
                qr=await client.login.get_qrcode(QRLoginType.MOBILE)
                stream=client.login.checking_mobile_qrcode(qr,deadline=anyio.current_time()+self.lifetime)
                async with contextlib.aclosing(stream):
                    async for result in stream:
                        if self.cancelled.is_set(): return
                        self.qr='data:'+qr.mimetype+';base64,'+base64.b64encode(qr.data).decode()
                        self.ready.set()
                        state={'SCAN':'pending','CONF':'scanned','DONE':'complete','REFUSE':'cancelled','TIMEOUT':'expired'}.get(result.event.name,'failed')
                        self.on_result(state,result.credential.model_dump() if result.credential else None)
                        if state in ('complete','cancelled','expired','failed'): return
                self.on_result('failed',None)
