param([int]$Port = 8877)
$ErrorActionPreference = 'Stop'
python -m smart_store_control.server --host 127.0.0.1 --port $Port
