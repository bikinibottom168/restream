"""Interface language.

A deliberately small translation layer: the English string in the template *is*
the lookup key, so an untranslated string still renders correctly instead of
showing a raw key like ``nav.dashboard``.

    {{ t('Dashboard') }}   ->  'แดชบอร์ด'   when the language is Thai
                           ->  'Dashboard'  when it is English, or when the
                                            phrase has no translation yet

The language is one application-wide setting (``ui_language``) rather than a
per-request negotiation, because this dashboard has a single operator.
"""

from __future__ import annotations

from typing import Any, Callable

DEFAULT_LANGUAGE = "th"

#: code -> label shown in the switcher
LANGUAGES: dict[str, str] = {
    "en": "English",
    "th": "ไทย",
}

#: English source string -> Thai. Anything missing falls back to English.
THAI: dict[str, str] = {
    # ---- navigation ---------------------------------------------------
    "Dashboard": "แดชบอร์ด",
    "Providers": "แหล่งสัญญาณ",
    "Events": "เหตุการณ์",
    "History": "ประวัติการล่ม",
    "Logs": "บันทึก",
    "Settings": "ตั้งค่า",
    "Setup": "ตั้งค่าเริ่มต้น",
    "Language": "ภาษา",
    # ---- status chrome ------------------------------------------------
    "OK": "พร้อม",
    "MISSING": "ไม่พบ",
    "ON": "เปิด",
    "OFF": "ปิด",
    "Startup warnings": "คำเตือนตอนเริ่มระบบ",
    # ---- summary cards ------------------------------------------------
    "Total": "ทั้งหมด",
    "Online": "ออนไลน์",
    "Offline": "ออฟไลน์",
    "Reconnecting": "กำลังเชื่อมต่อใหม่",
    "Disabled": "ปิดใช้งาน",
    # ---- system stats -------------------------------------------------
    "CPU": "ซีพียู",
    "RAM": "หน่วยความจำ",
    "FFmpeg processes": "จำนวน FFmpeg",
    "App uptime": "เวลาทำงานของระบบ",
    "System metrics unavailable on this host.": "เครื่องนี้อ่านค่าระบบไม่ได้",
    # ---- dashboard toolbar --------------------------------------------
    "Channels": "ช่องสัญญาณ",
    "Sync Channels": "ซิงก์รายการช่อง",
    "Start All": "เริ่มทั้งหมด",
    "Stop All": "หยุดทั้งหมด",
    "Restart Selected": "รีสตาร์ตที่เลือก",
    "Refresh Selected": "รีเฟรช URL ที่เลือก",
    "Bulk Add": "เพิ่มทีละหลายช่อง",
    "Add Channel": "เพิ่มช่อง",
    "Search channel...": "ค้นหาช่อง...",
    "All": "ทั้งหมด",
    "Filter": "กรอง",
    # ---- channel table ------------------------------------------------
    "Channel": "ช่อง",
    "Status": "สถานะ",
    "Source": "ต้นทาง",
    "RTMP": "ปลายทาง RTMP",
    "Uptime": "เวลาออนไลน์",
    "Bitrate": "บิตเรต",
    "Last check": "ตรวจล่าสุด",
    "Restarts": "รีสตาร์ต",
    "Actions": "การจัดการ",
    "No channels match this filter.": "ไม่มีช่องที่ตรงกับตัวกรองนี้",
    "Configure a provider": "ตั้งค่าแหล่งสัญญาณ",
    # ---- actions ------------------------------------------------------
    "Start": "เริ่ม",
    "Stop": "หยุด",
    "Restart": "รีสตาร์ต",
    "Refresh source": "รีเฟรช URL ต้นทาง",
    "Refresh Source": "รีเฟรช URL ต้นทาง",
    "Test Source": "ทดสอบต้นทาง",
    "Details": "รายละเอียด",
    "Edit": "แก้ไข",
    "Delete": "ลบ",
    "Enable": "เปิดใช้งาน",
    "Disable": "ปิดใช้งาน",
    "Save": "บันทึก",
    "Cancel": "ยกเลิก",
    "Create": "สร้าง",
    "Preview": "ดูตัวอย่าง",
    "Close": "ปิด",
    # ---- channel detail -----------------------------------------------
    "Input": "ขาเข้า",
    "Output": "ขาออก",
    "Provider": "แหล่งสัญญาณ",
    "Provider channel id": "รหัสช่องของแหล่งสัญญาณ",
    "Endpoint URL": "URL ปลายทางที่ใช้ดึง",
    "Current source": "URL ต้นทางปัจจุบัน",
    "Last refresh": "รีเฟรชล่าสุด",
    "Expires": "หมดอายุ",
    "Resolve count": "จำนวนครั้งที่ดึง URL",
    "Source status": "สถานะต้นทาง",
    "RTMP destination": "ปลายทาง RTMP",
    "Mode": "โหมด",
    "FFmpeg status": "สถานะ FFmpeg",
    "FFmpeg PID": "FFmpeg PID",
    "Output time": "เวลาที่ส่งออก",
    "Speed": "ความเร็ว",
    "Last progress": "ความคืบหน้าล่าสุด",
    "Started": "เริ่มเมื่อ",
    "Recent events": "เหตุการณ์ล่าสุด",
    "All events": "ดูทั้งหมด",
    "Downtime history": "ประวัติการล่ม",
    "Full history": "ดูประวัติทั้งหมด",
    "FFmpeg log (tail)": "บันทึก FFmpeg (ท้ายไฟล์)",
    "Open log viewer": "เปิดหน้าบันทึก",
    "Last error:": "ข้อผิดพลาดล่าสุด:",
    "running": "กำลังทำงาน",
    "stopped": "หยุดอยู่",
    "not configured": "ยังไม่ได้ตั้งค่า",
    "ago": "ที่แล้ว",
    # ---- providers page -----------------------------------------------
    "Add Provider": "เพิ่มแหล่งสัญญาณ",
    "Examples": "ตัวอย่าง",
    "Use this example": "ใช้ตัวอย่างนี้",
    "Fill in the example": "เติมค่าตัวอย่าง",
    "Show it as text": "ดูแบบข้อความ",
    "Test Authentication": "ทดสอบการเข้าสู่ระบบ",
    "Test Channel List": "ทดสอบรายการช่อง",
    "Test Stream Resolver": "ทดสอบการดึง URL",
    "Debug": "ตรวจสอบปัญหา",
    "Credentials": "ข้อมูลเข้าสู่ระบบ",
    "Configuration": "การตั้งค่า",
    "Provider name": "ชื่อแหล่งสัญญาณ",
    "Provider type": "ประเภท",
    "Authentication": "การยืนยันตัวตน",
    "Base URL": "Base URL",
    "Username": "ชื่อผู้ใช้",
    "Password": "รหัสผ่าน",
    "password": "รหัสผ่าน",
    "API token": "API token",
    "Cookie": "Cookie",
    "Enabled": "เปิดใช้งาน",
    "Default": "ค่าเริ่มต้น",
    "Discovery": "ดึงรายการช่องได้",
    "supported": "รองรับ",
    "not available": "ไม่รองรับ",
    "No providers yet.": "ยังไม่มีแหล่งสัญญาณ",
    "Save provider": "บันทึกแหล่งสัญญาณ",
    "Edit raw JSON": "แก้ไข JSON โดยตรง",
    # ---- events / history / logs --------------------------------------
    "Time": "เวลา",
    "Event": "เหตุการณ์",
    "Message": "ข้อความ",
    "All channels": "ทุกช่อง",
    "Select channel": "เลือกช่อง",
    "No events recorded yet.": "ยังไม่มีเหตุการณ์",
    "Down at": "ล่มเมื่อ",
    "Recovered at": "กลับมาเมื่อ",
    "Downtime": "ระยะเวลาที่ล่ม",
    "Cause": "สาเหตุ",
    "Attempts": "จำนวนครั้งที่ลอง",
    "Outages": "จำนวนครั้งที่ล่ม",
    "Total downtime": "รวมเวลาที่ล่ม",
    "Last 7 days": "7 วันล่าสุด",
    "No outages recorded.": "ยังไม่มีประวัติการล่ม",
    "ongoing": "ยังล่มอยู่",
    "Application": "ระบบ",
    "Show": "แสดง",
    "Nothing logged yet.": "ยังไม่มีบันทึก",
    "lines": "บรรทัด",
    # ---- settings ------------------------------------------------------
    "Monitoring": "การตรวจสอบ",
    "Recovery": "การกู้คืน",
    "Streaming": "การสตรีม",
    "Binaries": "โปรแกรมที่ใช้",
    "Telegram": "Telegram",
    "Security & privacy": "ความปลอดภัยและความเป็นส่วนตัว",
    "Save settings": "บันทึกการตั้งค่า",
    "Export Configuration": "ส่งออกการตั้งค่า",
    "Import Configuration": "นำเข้าการตั้งค่า",
    "Send Test Message": "ส่งข้อความทดสอบ",
    "Bot token": "Bot token",
    "Chat id": "Chat id",
    "Default RTMP server": "RTMP server หลัก",
    "Default stream mode": "โหมดสตรีมเริ่มต้น",
    "FFmpeg path": "ตำแหน่ง FFmpeg",
    "ffprobe path": "ตำแหน่ง ffprobe",
    "Dashboard password": "รหัสผ่านแดชบอร์ด",
    # ---- bulk add ------------------------------------------------------
    "Bulk add channels": "เพิ่มช่องทีละหลายรายการ",
    "Create channels": "สร้างช่องทั้งหมด",
    "Load JSON file": "โหลดไฟล์ JSON",
    "Stream key prefix": "คำนำหน้า stream key",
    "Stream mode": "โหมดสตรีม",
    "Name": "ชื่อ",
    "Type": "ประเภท",
    "Key": "Key",
    # ---- IPTV easy form -------------------------------------------------
    "Add IPTV source": "เพิ่มแหล่ง IPTV",
    "Advanced provider": "แบบละเอียด",
    "IPTV source": "แหล่ง IPTV",
    "Source name": "ชื่อแหล่ง",
    "This source needs a login": "แหล่งนี้ต้องล็อกอิน",
    "Login": "การเข้าสู่ระบบ",
    "The page that accepts the username and password.":
        "หน้าที่รับชื่อผู้ใช้และรหัสผ่าน",
    "Advanced login options": "ตัวเลือกล็อกอินเพิ่มเติม",
    "Test login": "ทดสอบล็อกอิน",
    "Anti-drop buffer": "บัฟเฟอร์กันหลุด",
    "MediaMTX log": "บันทึก MediaMTX",
    "Start on boot": "เปิดอัตโนมัติเมื่อเปิดเครื่อง",
    "on": "เปิด",
    "Starts the app automatically when the computer boots (after login) and restarts it if it ever crashes. No administrator rights needed.": "เปิดโปรแกรมอัตโนมัติเมื่อเปิดเครื่อง (หลังล็อกอิน) และรีสตาร์ทให้เองถ้าโปรแกรมหลุด ไม่ต้องใช้สิทธิ์ admin",
    "Install auto-start": "ติดตั้งให้เปิดออโต้",
    "Remove auto-start": "ลบการเปิดออโต้",
    "Auto-start is available on Windows and macOS.": "การเปิดอัตโนมัติรองรับบน Windows และ macOS",
    "running": "กำลังทำงาน",
    "stopped": "หยุด",
    "off": "ปิด",
    "Keep viewers connected through source dropouts (MediaMTX)": "ให้ผู้ชมไม่หลุดตอนต้นทางสะดุด (MediaMTX)",
    "Viewers watch from a local buffer, so a short source dropout is invisible and the player never disconnects.": "ผู้ชมดูจากบัฟเฟอร์ในเครื่อง ต้นทางหลุดสั้นๆ จะมองไม่เห็น และเครื่องเล่นจะไม่หลุด",
    "Buffer / delay (seconds)": "บัฟเฟอร์ / ดีเลย์ (วินาที)",
    "Viewers play this far behind live.": "ผู้ชมจะช้ากว่าสดเท่านี้",
    "Viewer host / IP": "โฮสต์ / IP สำหรับผู้ชม",
    "The address players use to reach this machine.": "ที่อยู่ที่เครื่องเล่นใช้ต่อมายังเครื่องนี้",
    "Show a \"reconnecting\" screen on a long outage": "แสดงหน้า \"กำลังเชื่อมต่อใหม่\" ตอนหลุดยาว",
    "RTMP port": "พอร์ต RTMP",
    "HLS port": "พอร์ต HLS",
    "API port": "พอร์ต API",
    "MediaMTX path (optional)": "ตำแหน่ง MediaMTX (ถ้ามี)",
    "MediaMTX found:": "พบ MediaMTX:",
    "MediaMTX not found. Download": "ไม่พบ MediaMTX ดาวน์โหลด",
    "and place the binary at": "แล้ววางไฟล์ไว้ที่",
    "Watch links (buffered)": "ลิงก์สำหรับดู (ผ่านบัฟเฟอร์)",
    "Best for staying connected through dropouts.": "เหมาะที่สุดสำหรับไม่ให้หลุดตอนสัญญาณขาด",
    "Point VLC at the HLS link. It keeps playing from the buffer even while the source reconnects.": "ใช้ลิงก์ HLS ใน VLC จะเล่นจากบัฟเฟอร์ต่อได้แม้ต้นทางกำลังเชื่อมต่อใหม่",
    "Stop the slate after (seconds)": "หยุดสเลทหลังหลุดเกิน (วินาที)",
    "Reconnecting image (optional)": "รูปตอนเชื่อมต่อใหม่ (ถ้ามี)",
    "Shown during a long outage. Leave empty for a plain dark screen.": "แสดงตอนหลุดยาว เว้นว่างไว้จะเป็นจอมืดเรียบๆ",
    "Login form page (if different)": "หน้าที่แสดงฟอร์มล็อกอิน (ถ้าคนละที่)",
    "The page that shows the login form and its CSRF field, if it is not the same URL the form submits to. Leave empty to use the login URL.": "หน้าที่แสดงฟอร์มล็อกอินและค่า CSRF ถ้าไม่ใช่ URL เดียวกับที่ฟอร์มส่งไป เว้นว่างเพื่อใช้ URL ล็อกอิน",
    "Where the form SUBMITS to (the action URL), e.g. /authen. The CSRF token is found automatically.": "URL ที่ฟอร์มส่งไป (ค่า action) เช่น /authen ระบบจะหา CSRF ให้อัตโนมัติ",
    "Usually leave empty - the form page (and its CSRF) is found automatically, including the site home page. Only set this to force a specific page.": "ปกติเว้นว่างได้ - ระบบจะหาหน้าฟอร์ม (และ CSRF) ให้เอง รวมถึงหน้าแรกของเว็บ ตั้งค่านี้เฉพาะเมื่อต้องการบังคับหน้าที่เจาะจง",
    "Page after a successful login": "หน้าที่ไปเมื่อล็อกอินสำเร็จ",
    "If login lands here (not back on the login page), it counts as success. e.g. /main": "ถ้าล็อกอินแล้วมาที่หน้านี้ (ไม่เด้งกลับหน้า login) ถือว่าสำเร็จ เช่น /main",

    "Username field": "ชื่อฟิลด์ username",
    "Password field": "ชื่อฟิลด์ password",
    "Stream URL JSON path (optional)": "JSON path ของ URL stream (ไม่บังคับ)",
    "Where the media URL sits in the response. Leave empty to auto-detect.":
        "ตำแหน่งของ URL สื่อใน response เว้นว่างเพื่อให้ระบบหาเอง",
    "One URL per channel. Name each one yourself.":
        "หนึ่ง URL ต่อหนึ่งช่อง ตั้งชื่อเองได้",
    "Channel name": "ชื่อช่อง",
    "Test": "ทดสอบ",
    "Add row": "เพิ่มแถว",
    "Paste a list": "วางเป็นรายการ",
    "Add to the list": "เพิ่มเข้ารายการ",
    "Example: https://media.example.com/play?id=82290":
        "ตัวอย่าง: https://media.example.com/play?id=82290",
    "Getting the URL from the response": "การดึง URL จาก response",
    "Stream URL JSON field (optional)": "ชื่อ field ใน JSON ที่เก็บ URL (ไม่บังคับ)",
    "Which field in the JSON holds the URL, e.g. data.stream.url. Leave empty to search automatically.":
        "field ไหนใน JSON ที่เก็บ URL เช่น data.stream.url เว้นว่างเพื่อให้ระบบค้นเอง",
    "Preview the first URL": "ดู response ของ URL แรก",
    "See the response and pick the right field.":
        "ดู response แล้วเลือก field ที่ถูกต้อง",
    # ---- provider configuration fields (from config_schema) -------------
    "Base URL (optional)": "Base URL (ไม่บังคับ)",
    "Timeout (seconds)": "หมดเวลา (วินาที)",
    "Verify TLS certificates": "ตรวจสอบใบรับรอง TLS",
    "Login URL": "URL หน้าเข้าสู่ระบบ",
    "Login method": "เมธอดตอนล็อกอิน",
    "Login body": "รูปแบบข้อมูลตอนล็อกอิน",
    "Username field": "ชื่อฟิลด์ username",
    "Password field": "ชื่อฟิลด์ password",
    "Token JSON path": "JSON path ของ token",
    "Auth header name": "ชื่อ header สำหรับยืนยันตัวตน",
    "Auth header format": "รูปแบบ header สำหรับยืนยันตัวตน",
    "Login failure marker": "ข้อความที่แปลว่าล็อกอินไม่สำเร็จ",
    "Channels URL": "URL รายการช่อง",
    "Channels JSON path": "JSON path ของรายการช่อง",
    "Channel id field": "ชื่อฟิลด์รหัสช่อง",
    "Channel name field": "ชื่อฟิลด์ชื่อช่อง",
    "Channel logo field": "ชื่อฟิลด์โลโก้",
    "Stream resolve URL": "URL สำหรับดึง stream",
    "Resolve method": "เมธอดตอนดึง",
    "Response parser": "วิธีอ่าน response",
    "Stream URL JSON path": "JSON path ของ URL stream",
    "Expiry JSON path": "JSON path ของเวลาหมดอายุ",
    "Send session cookies to FFmpeg": "ส่ง cookie ของ session ให้ FFmpeg",
    "User-Agent": "User-Agent",
    "Extra API headers (JSON)": "Header เพิ่มเติมสำหรับ API (JSON)",
    "Playback headers (JSON)": "Header ตอนเล่น (JSON)",
    "Request headers (JSON)": "Header ตอนยิง request (JSON)",
    "Extra headers (JSON)": "Header เพิ่มเติม (JSON)",
    "URL template": "รูปแบบ URL (template)",
    "Playlist URL (M3U)": "URL playlist (M3U)",
    "Playlist cache (seconds)": "แคช playlist (วินาที)",
    "Referer": "Referer",
    # ---- provider field help text ---------------------------------------
    "Scheme + host of your source. Every path below is relative to it.":
        "scheme + host ของต้นทาง ทุก path ด้านล่างจะอ้างอิงจากตรงนี้",
    "Pick 'none' if the endpoint needs no credentials.":
        "เลือก 'none' ถ้า endpoint ไม่ต้องใช้ข้อมูลเข้าสู่ระบบ",
    "Relative to the base URL. Only used by 'form' authentication.":
        "เป็น path ต่อจาก Base URL ใช้เฉพาะการยืนยันตัวตนแบบ form",
    "The form field name your login page expects.":
        "ชื่อฟิลด์ที่หน้าล็อกอินของคุณต้องการ",
    "Where to find a token in the login response, if it returns one.":
        "ตำแหน่งของ token ใน response ตอนล็อกอิน (ถ้ามี)",
    "Text that appears when login fails but the server still answers 200.":
        "ข้อความที่ขึ้นเมื่อล็อกอินไม่ผ่าน แต่เซิร์ฟเวอร์ยังตอบ 200",
    "Leave empty if you add channels by hand. Fill it to enable Sync Channels.":
        "เว้นว่างได้ถ้าเพิ่มช่องเอง ใส่เมื่อต้องการใช้ปุ่มซิงก์รายการช่อง",
    "Where the array of channels sits. Leave empty to auto-detect.":
        "ตำแหน่งของ array รายการช่อง เว้นว่างเพื่อให้ระบบหาเอง",
    "{channel_id} is replaced with the channel's provider id.":
        "{channel_id} จะถูกแทนด้วยรหัสช่องของแหล่งสัญญาณ",
    "{channel_id} is replaced with each channel's provider id.":
        "{channel_id} จะถูกแทนด้วยรหัสช่องของแต่ละช่อง",
    "'auto' handles JSON, HTML and JavaScript. Use 'json_path' to be strict.":
        "'auto' อ่านได้ทั้ง JSON, HTML และ JavaScript ใช้ 'json_path' เมื่อต้องการระบุชัดเจน",
    "'auto' handles JSON, HTML and JavaScript. 'json_path' is strict.":
        "'auto' อ่านได้ทั้ง JSON, HTML และ JavaScript ส่วน 'json_path' จะระบุชัดเจน",
    "Dotted path. Supports items[0].url and items[*].url.":
        "เขียนแบบคั่นจุด รองรับ items[0].url และ items[*].url",
    "Optional. Unix timestamp or ISO date telling us when the URL dies.":
        "ไม่บังคับ ใส่ unix timestamp หรือวันที่แบบ ISO เพื่อบอกว่า URL หมดอายุเมื่อไร",
    "Sent to FFmpeg when it fetches the media itself.":
        "ส่งให้ FFmpeg ตอนดึงไฟล์สื่อ",
    "Sent to FFmpeg when fetching the source.":
        "ส่งให้ FFmpeg ตอนดึงต้นทาง",
    "Sent both when fetching the endpoint and by FFmpeg.":
        "ส่งทั้งตอนยิงไป endpoint และตอน FFmpeg ดึงสื่อ",
    "Sent with every API request. Masked everywhere it is displayed.":
        "ส่งไปกับทุก request ของ API และถูกปิดบังทุกที่ที่แสดงผล",
    "Sent with the media request only, not with the API calls.":
        "ส่งเฉพาะตอนขอไฟล์สื่อ ไม่ได้ส่งตอนเรียก API",
    "Sent when fetching the endpoint. Masked everywhere it is shown.":
        "ส่งตอนยิงไป endpoint และถูกปิดบังทุกที่ที่แสดงผล",
    "Sent by FFmpeg with the media request.":
        "FFmpeg จะส่งไปพร้อมกับการขอไฟล์สื่อ",
    "Masked everywhere it is displayed.": "ถูกปิดบังทุกที่ที่แสดงผล",
    "Optional. Prefixed to a relative URL template.":
        "ไม่บังคับ ใช้เติมหน้า URL template ที่เป็น path",
    "Optional. Enables Sync Channels for this provider.":
        "ไม่บังคับ เปิดให้ใช้ปุ่มซิงก์รายการช่องของแหล่งนี้",
    "Only needed if you want to enter relative paths on channels.":
        "ใส่เมื่อต้องการกรอก URL แบบ path สั้น ๆ ในแต่ละช่อง",
    "Which field holds the URL. Leave empty to search the whole response.":
        "ฟิลด์ที่เก็บ URL เว้นว่างเพื่อค้นทั้ง response",
    # ---- longer prose on the providers page ------------------------------
    "A provider tells the system how to obtain a playable URL for a channel.":
        "แหล่งสัญญาณคือตัวบอกระบบว่าจะไปเอา URL ที่เล่นได้ของแต่ละช่องมาจากไหน",
    "Credentials are stored in the OS keychain, never in the database and never in a log file.":
        "ข้อมูลเข้าสู่ระบบถูกเก็บใน keychain ของเครื่อง ไม่เก็บในฐานข้อมูลและไม่เขียนลงบันทึก",
    "Leave a field empty to keep the stored value. Stored values are never displayed.":
        "เว้นว่างไว้เพื่อใช้ค่าเดิมที่เก็บอยู่ ค่าที่เก็บไว้จะไม่ถูกแสดงออกมา",
    "Add one to resolve channel URLs automatically, or skip this entirely and paste a URL per channel on the dashboard.":
        "เพิ่มไว้เพื่อให้ระบบดึง URL ให้อัตโนมัติ หรือข้ามไปเลยแล้ววาง URL เองรายช่องบนแดชบอร์ดก็ได้",
    "Press Use this example to open the form already filled in, then change the host and the field names to match your source.":
        "กด ใช้ตัวอย่างนี้ เพื่อเปิดฟอร์มที่กรอกค่าไว้แล้ว จากนั้นแก้ host และชื่อฟิลด์ให้ตรงกับต้นทางของคุณ",
    "Nothing is saved until you press Save.": "จะยังไม่บันทึกจนกว่าคุณจะกดบันทึก",
    "Advanced: this overrides the fields above when saved.":
        "สำหรับผู้ใช้ขั้นสูง ค่านี้จะทับค่าจากช่องด้านบนเมื่อบันทึก",
    "One channel per line, or paste a JSON file.":
        "หนึ่งช่องต่อหนึ่งบรรทัด หรือวางไฟล์ JSON ก็ได้",
    "Lines starting with # are ignored.": "บรรทัดที่ขึ้นต้นด้วย # จะถูกข้าม",
    "Endpoint URLs need a provider that fetches them.":
        "URL แบบ endpoint ต้องมีแหล่งสัญญาณคอยไปดึงให้",
    "Fills empty keys: sport01, sport02, ...":
        "เติม key ที่เว้นว่างให้: sport01, sport02, ...",
    "Direct media URL, used as-is.": "URL ไฟล์สื่อโดยตรง ใช้ตามที่กรอกเลย",
    "A direct media URL: .m3u8, .mpd, .ts, rtmp://, srt://":
        "URL ไฟล์สื่อโดยตรง เช่น .m3u8, .mpd, .ts, rtmp://, srt://",
    "Appended to the default RTMP server from Settings.":
        "จะถูกต่อท้าย RTMP server หลักที่ตั้งไว้ในหน้าตั้งค่า",
    "Passed to the provider unchanged. Leave empty for manual URLs.":
        "ส่งให้แหล่งสัญญาณตามที่กรอก เว้นว่างได้ถ้าใส่ URL เอง",
    "or Stream key": "หรือ Stream key",
    "Provider channel id": "รหัสช่องของแหล่งสัญญาณ",
    # ---- channel form --------------------------------------------------
    "Source URL (media)": "URL ต้นทาง (ไฟล์สื่อโดยตรง)",
    "Source endpoint URL": "URL ปลายทางที่ใช้ดึง",
    "RTMP URL (full)": "RTMP URL (แบบเต็ม)",
    "Stream key": "Stream key",
    "Auto start": "เริ่มอัตโนมัติ",
    "Logo URL": "URL โลโก้",
    "Group": "กลุ่ม",
    "Playback Referer": "Referer ตอนเล่น",
    "Playback User-Agent": "User-Agent ตอนเล่น",
    "Copy": "คัดลอก",
}

TRANSLATIONS: dict[str, dict[str, str]] = {"th": THAI}


def normalise(code: str | None) -> str:
    """Return a supported language code, defaulting when unknown."""
    value = (code or "").strip().lower()
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def translate(text: str, language: str = DEFAULT_LANGUAGE) -> str:
    """Look up one phrase, falling back to the English source."""
    table = TRANSLATIONS.get(normalise(language))
    if not table:
        return text
    return table.get(text, text)


def make_translator(language: str) -> Callable[[str], str]:
    """Return a ``t(text)`` bound to *language*, for use as a Jinja global."""
    code = normalise(language)
    table = TRANSLATIONS.get(code, {})

    def _t(text: str) -> str:
        return table.get(text, text)

    return _t


def translate_schema(
    schema: list[dict[str, Any]], language: str = DEFAULT_LANGUAGE
) -> list[dict[str, Any]]:
    """Translate the ``label``/``help`` of provider configuration fields.

    The schema is written in English inside each provider class; this renders
    it in the operator's language without the providers having to know that a
    translation layer exists.
    """
    code = normalise(language)
    if code == "en":
        return schema
    table = TRANSLATIONS.get(code, {})
    translated: list[dict[str, Any]] = []
    for field in schema:
        copy = dict(field)
        for key in ("label", "help"):
            value = copy.get(key)
            if isinstance(value, str) and value in table:
                copy[key] = table[value]
        translated.append(copy)
    return translated


def translate_provider_types(
    entries: list[dict[str, Any]], language: str = DEFAULT_LANGUAGE
) -> list[dict[str, Any]]:
    """Translate the schema inside each entry of ``ProviderFactory.available()``."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        copy = dict(entry)
        if isinstance(copy.get("schema"), list):
            copy["schema"] = translate_schema(copy["schema"], language)
        out.append(copy)
    return out


def coverage(language: str = "th") -> float:
    """Fraction of known phrases translated - used by the test suite."""
    table = TRANSLATIONS.get(normalise(language), {})
    if not table:
        return 0.0
    translated = sum(1 for key, value in table.items() if value and value != key)
    return translated / len(table)
