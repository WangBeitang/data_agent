import asyncio
from _contextvars import ContextVar

from pygments.styles import default


# 创建一个用来存储请求id的ContextVar对象

_req_context_var = ContextVar("req_id", default="")

# 保存请求id
def set_req_id(request_id: str):
    return _req_context_var.set(request_id)

# 获取请求id
def get_req_id() -> str:
    return _req_context_var.get()

if __name__ == "__main__":
    async def req1():
        print(f"请求1开始准备执行： req_id={_req_context_var.get()}")  # 0
        _req_context_var.set('111')
        print(f"请求1执行完毕： req_id={_req_context_var.get()}") # 1

    async def req12():
        print(f"请求2开始准备执行： req_id={_req_context_var.get()}")  # 0
        _req_context_var.set('222')
        print(f"请求2执行完毕： req_id={_req_context_var.get()}") # 2


    def test2():
        print(f"----{_req_context_var.get()}")  # 0
        _req_context_var.set('333')
        print(f"----{_req_context_var.get()}")  # 3
        _req_context_var.set('444')
        print(f"----{_req_context_var.get()}") #4


    async def test():
        cor1 = req1()
        cor2 = req12()
        await asyncio.gather(cor1, cor2)

    asyncio.run(test())
    # test2()


