# One-time setup: CUDA training venv (NVIDIA GPU) so tooltip retrains run
# in minutes instead of ~1h on CPU. The MAIN python install stays CPU-only
# (keeps the packaged EXE small). Progress goes to pip_gpu.log; the last
# line is GPU_VENV_READY on success.
$ErrorActionPreference = "Continue"
Set-Location "D:\Addon test"
$vp  = "D:\Addon test\tooltip_gpu_venv\Scripts\python.exe"
$log = "D:\Addon test\pip_gpu.log"

"[$(Get-Date)] setup started" | Out-File $log -Encoding utf8

"[$(Get-Date)] checking venv pip" | Out-File $log -Append -Encoding utf8
& $vp -m pip --version *>> $log
if ($LASTEXITCODE -ne 0) {
    "PIP_MISSING_IN_VENV" | Out-File $log -Append -Encoding utf8
    & $vp -m ensurepip --upgrade *>> $log
    if ($LASTEXITCODE -ne 0) { "ENSUREPIP_FAILED" | Out-File $log -Append -Encoding utf8; exit 1 }
}

"[$(Get-Date)] upgrading pip" | Out-File $log -Append -Encoding utf8
& $vp -m pip install --upgrade pip *>> $log

"[$(Get-Date)] installing torch 2.14.1+cu126 + torchvision (large download)" | Out-File $log -Append -Encoding utf8
& $vp -m pip install "torch==2.14.1+cu126" torchvision --index-url https://download.pytorch.org/whl/cu126 *>> $log
if ($LASTEXITCODE -ne 0) { "TORCH_INSTALL_FAILED" | Out-File $log -Append -Encoding utf8; exit 1 }

"[$(Get-Date)] installing ultralytics" | Out-File $log -Append -Encoding utf8
& $vp -m pip install ultralytics *>> $log
if ($LASTEXITCODE -ne 0) { "ULTRALYTICS_INSTALL_FAILED" | Out-File $log -Append -Encoding utf8; exit 1 }

"[$(Get-Date)] smoke-testing CUDA" | Out-File $log -Append -Encoding utf8
& $vp -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('cuda ok:', torch.cuda.get_device_name(0))" *>> $log
if ($LASTEXITCODE -ne 0) { "CUDA_SMOKE_TEST_FAILED" | Out-File $log -Append -Encoding utf8; exit 1 }

"GPU_VENV_READY" | Out-File $log -Append -Encoding utf8
