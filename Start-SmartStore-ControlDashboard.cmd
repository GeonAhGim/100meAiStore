@echo off
cd /d "C:\smart_store"
start "smart_store-control-dashboard" /min python.exe -m smart_store_control.server --host 127.0.0.1 --port 8877
