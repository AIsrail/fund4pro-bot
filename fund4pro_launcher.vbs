' fund4pro (grant_bizplan_bot) — standalone aiogram Telegram bot launcher.
' Runs completely independently of Hermes Desktop / Hermes gateway — it
' is a separate Python process that owns the fund4pro Telegram bot token.
' Launched hidden (no visible console window) on Windows login.
Option Explicit
Dim sh, botDir
botDir = "C:\Users\user\Downloads\grant_bizplan_bot_v2_extracted\grant_bizplan_bot"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = botDir
sh.Run "cmd.exe /c cd /d """ & botDir & """ && python run_with_env.py >> fund4pro.log 2>&1", 0, False
