"""读者附件领域：照片与证件影像的上传、鉴权下载与删除。

文件落 ``LIBRARY_UPLOAD_DIR``（默认与数据库同目录的 ``uploads/``），
库里只存随机存储名，不存绝对路径。
"""

from library.memberfiles.routes import bp

__all__ = ["bp"]
