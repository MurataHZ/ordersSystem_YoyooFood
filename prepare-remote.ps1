$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location -LiteralPath $PSScriptRoot

function Get-OfficialFile {
    param([string]$Url, [string]$Destination)
    $partialPath = $Destination + '.download'
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $partialPath -UseBasicParsing -TimeoutSec 180
            if ((Get-Item -LiteralPath $partialPath).Length -eq 0) { throw '下载的文件为空。' }
            Move-Item -LiteralPath $partialPath -Destination $Destination -Force
            return
        } catch {
            Remove-Item -LiteralPath $partialPath -ErrorAction SilentlyContinue
            if ($attempt -eq 3) { throw }
            Write-Host "下载未完成，正在重试（$attempt / 3）……"
        }
    }
}

try {
    $runtimeDirectory = Join-Path $PSScriptRoot 'runtime'
    $pythonDirectory = Join-Path $runtimeDirectory 'python'
    $pythonExecutable = Join-Path $pythonDirectory 'python.exe'
    $tunnelExecutable = Join-Path $runtimeDirectory 'cloudflared.exe'
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

    if ([Environment]::Is64BitOperatingSystem) {
        $pythonArchitecture = 'amd64'
        $tunnelArchitecture = 'amd64'
        $pythonChecksum = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
    } else {
        $pythonArchitecture = 'win32'
        $tunnelArchitecture = '386'
        $pythonChecksum = '4afd37bc88ed1391209a4c58deb902cd0741a476692412b23d340aff6e9285e7'
    }

    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        Write-Host '[1/3] 正在下载 Python 便携运行包，无需安装到系统。'
        $pythonArchive = Join-Path $runtimeDirectory 'python-portable.zip'
        Get-OfficialFile -Url "https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-$pythonArchitecture.zip" -Destination $pythonArchive
        if ((Get-FileHash -LiteralPath $pythonArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonChecksum) {
            Remove-Item -LiteralPath $pythonArchive -Force
            throw 'Python 文件校验未通过，请重新启动以重新下载。'
        }
        Expand-Archive -LiteralPath $pythonArchive -DestinationPath $pythonDirectory -Force
        Remove-Item -LiteralPath $pythonArchive -Force
    } else {
        Write-Host '[1/3] Python 运行包已准备好。'
    }

    & $pythonExecutable -c "import sqlite3, http.server, ssl; print('Python runtime OK')"
    if ($LASTEXITCODE -ne 0) { throw 'Python 无法运行。请检查是否为正常更新过的 Windows 10 Intel/AMD 电脑。' }

    if (-not (Test-Path -LiteralPath $tunnelExecutable)) {
        Write-Host '[2/3] 正在从 Cloudflare 官方发布页下载远程连接工具。'
        Get-OfficialFile -Url "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$tunnelArchitecture.exe" -Destination $tunnelExecutable
        $binaryStream = [IO.File]::OpenRead($tunnelExecutable)
        try { $isExecutable = ($binaryStream.ReadByte() -eq 77) -and ($binaryStream.ReadByte() -eq 90) }
        finally { $binaryStream.Dispose() }
        if (-not $isExecutable) {
            Remove-Item -LiteralPath $tunnelExecutable -Force
            throw '远程连接工具下载不完整，请重新启动。'
        }
    } else {
        Write-Host '[2/3] 远程连接工具已准备好。'
    }

    Write-Host '[3/3] 正在启动订单系统与远程连接……'
    & $pythonExecutable (Join-Path $PSScriptRoot 'remote_launcher.py')
    exit $LASTEXITCODE
} catch {
    Write-Host ''
    Write-Host ('准备失败：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '请先确认这台电脑可以正常上网。下载失败时也可参考说明手动下载组件。'
    exit 1
}
