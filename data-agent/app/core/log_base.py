"""
封装日志模块
# 定制日志输出格式，控制输出级别
# 自动将日志内容保存到日志文件
# 指定日志文件的最大大小，一旦超过自动创建一个新的的文件
# 指定文件的有效期，过了自动删除
# 记录当前日志输出是哪个请求的，记录请求id
"""
import sys
from loguru import logger

from app.core.context import get_req_id, set_req_id

# 配置日志格式
log_format = (
    "<red>{time:YYYY-MM-DD HH:mm:ss.SSS}</red> | "  # 绿色显示日志时间（精确到毫秒）
    "<level>{level: <8}</level> | "  # 按级别颜色显示日志级别（左对齐，占8个字符）
    "<magenta>request_id - {extra[request_id]}</magenta> | "  # 品红色显示request_id（从日志extra中获取）
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "  # 青色显示日志所在文件、函数、行号
    "<level>{message}</level>"  # 按级别颜色显示日志正文
)

def inject_request_id(record):
    print('---------')
    request_id = get_req_id()
    record["extra"]["request_id"] = request_id

# 移除默认的日志配置
logger.remove()

# 给日志打补丁，使其在输出每条日志前执行inject_request_id函数，注入request_id
logger = logger.patch(inject_request_id)

# 添加控制台日志输出的配置
logger.add(sink=sys.stdout, level="INFO", format=log_format)
# 添加文件日志输出的配置
logger.add(
    sink="test.log",
    level="DEBUG",
    format=log_format,
    rotation="1 kB",
    retention="2 day" # 有效时长2天
)

if __name__ == '__main__':
    set_req_id(4)
    logger.debug("debug info...")
    logger.info("info info...")
    logger.warning("warning info...")
    logger.error("error info...")