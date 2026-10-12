"""批量导入领域：图书与读者的 CSV 批量建档（含预览、逐行报错与模板下载）。

逐行独立处理，单行失败不影响其他行；本模块不持有自己的表，写入完全复用
``books`` / ``members`` 的 service，保证「单个新增」与「批量导入」口径一致。
"""

from library.importer.routes import bp

__all__ = ["bp"]
