# Roda o robô no PC: baixa a planilha do Keeper, criptografa e publica no GitHub (Pages).
# Também publica status.json (sucesso ou falha) para o app e o histórico de commits mostrarem
# quando o robô rodou pela última vez.
#
# Uso manual:   powershell -NoProfile -ExecutionPolicy Bypass -File sync\rodar.ps1
# Agendado:     ver LEIA-ME.md (Agendador de Tarefas do Windows)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$log = Join-Path $PSScriptRoot "ultimo-log.txt"
Start-Transcript -Path $log -Force | Out-Null

$resultado = "erro"; $mensagem = ""
try {
    # Carrega as variáveis do .env (KEEPER_EMAIL, KEEPER_SENHA, DATA_KEY)
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) { throw "Arquivo .env não encontrado em $root" }
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process") }
    }

    # Chromium do Playwright e Python do ambiente virtual ficam no D:
    if (-not $env:PLAYWRIGHT_BROWSERS_PATH) { $env:PLAYWRIGHT_BROWSERS_PATH = "D:\sistemas\ms-playwright" }
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }

    # Logo depois de ligar/acordar o PC a rede pode demorar: espera até 3 min o Keeper responder (DNS)
    $rede = $false
    for ($i = 0; $i -lt 18 -and -not $rede; $i++) {
        try { [void][Net.Dns]::GetHostAddresses("conta.keeperformaturas.com.br"); $rede = $true }
        catch { Start-Sleep -Seconds 10 }
    }
    if (-not $rede) { throw "Sem internet/DNS para conta.keeperformaturas.com.br após 3 min" }

    Write-Host "[$(Get-Date -Format 'dd/MM/yyyy HH:mm')] Baixando planilha do Keeper..."
    # Roda o robô com prazo máximo: em 12/10 ele travou 2 horas (sem limite) e só morreu quando o Windows desligou.
    $env:PYTHONIOENCODING = "utf-8"
    $tmpOut = Join-Path $PSScriptRoot "saida-robo.tmp"; $tmpErr = Join-Path $PSScriptRoot "saida-robo-err.tmp"
    $proc = Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $PSScriptRoot 'sync.py')`"" -NoNewWindow -PassThru `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    $null = $proc.Handle   # sem isto o ExitCode vem vazio (null) com WaitForExit(timeout) e tudo parecia erro
    $terminou = $proc.WaitForExit(15 * 60 * 1000)
    if ($terminou) {
        $proc.WaitForExit()   # finaliza a leitura do código de saída
    } else {
        & taskkill /PID $proc.Id /T /F | Out-Null   # mata o Python e o Chromium que ele abriu
    }
    $saidaPy = @()
    foreach ($f in @($tmpOut, $tmpErr)) {
        if (Test-Path $f) { $saidaPy += Get-Content $f -Encoding UTF8 | Where-Object { $_ -and $_.Trim() }; Remove-Item $f -Force -ErrorAction SilentlyContinue }
    }
    $saidaPy | ForEach-Object { Write-Host $_ }
    if (-not $terminou) { throw "Tempo esgotado: o robô passou de 15 min e foi encerrado" }
    $okLinha = $saidaPy | Where-Object { $_ -match '^OK:' } | Select-Object -Last 1
    if ($proc.ExitCode -ne 0 -and -not $okLinha) { throw (($saidaPy | Select-Object -Last 1) -replace '\s+', ' ') }

    $resultado = "ok"
    $mensagem = ($saidaPy | Where-Object { $_ -match '^OK:' } | Select-Object -Last 1) -replace '^OK:\s*', ''
} catch {
    $mensagem = "$_"
    Write-Host "ERRO: $mensagem"
}

# status.json: sempre publicado, para o app e o histórico mostrarem a última execução
$status = @{
    ultimaExecucao = (Get-Date).ToString("o")
    resultado      = $resultado
    mensagem       = ($mensagem -replace '[\r\n]+', ' ').Substring(0, [Math]::Min(160, $mensagem.Length))
    maquina        = $env:COMPUTERNAME
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $root "status.json"), $status, (New-Object Text.UTF8Encoding $false))

try {
    Write-Host "Publicando no GitHub..."
    # --autostash: dados.enc/status.json já estão modificados aqui; sem isso o pull recusa quando há commit local à frente
    git pull -q --rebase --autostash origin main
    if ($LASTEXITCODE -ne 0) { throw "git pull falhou (código $LASTEXITCODE)" }
    git add dados.enc status.json
    if (git status --porcelain dados.enc status.json) {
        $prefixo = if ($resultado -eq "ok") { "dados" } else { "robô falhou" }
        git commit -q -m "${prefixo}: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        if ($LASTEXITCODE -ne 0) { throw "git commit falhou (código $LASTEXITCODE)" }
        git push -q origin main
        if ($LASTEXITCODE -ne 0) { throw "git push falhou (código $LASTEXITCODE)" }
        Write-Host "Publicado ($resultado)."
    }
} catch {
    Write-Host "ERRO ao publicar: $_"
    $resultado = "erro"
}

Stop-Transcript | Out-Null
if ($resultado -ne "ok") { exit 1 }
