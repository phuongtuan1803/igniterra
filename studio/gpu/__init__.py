"""GPU thuê on-demand (Gate 4, 09 · GPU thuê và ComfyUI).

Module hạ tầng, không phải provider nội dung: web chỉ gọi ``studio.gpu.service``; adapter cụ thể
được chọn bằng cấu hình ``[gpu] provider`` qua ``studio.gpu.adapters.get``. Không import ``app.*`` của MPT.
"""
