# DeepClip Mobile — Complete MVP Source

Mobile-first AI video recap web app with:
- Burmese / English / Thai recap output
- Video upload (MP4, MOV, MKV, WEBM)
- AI transcription + AI recap
- SRT subtitle export
- 9:16, 3:4, 16:9, 1:1 formatting
- Crop or blurred background
- AI-selected highlight clips (experimental MVP)
- User registration / login
- Free tier: 3 successful/in-progress video jobs per Myanmar calendar day
- Premium flag: bypasses the daily 3-job free limit
- Admin dashboard and manual Premium activation
- Responsive mobile UI

## Important: what is and is not included
This is a deployable MVP codebase, not a hosted public website. It needs your own server, OpenAI API key, and domain/hosting account. No payment provider is connected because payment gateways require the merchant account and credentials of the site owner. Admin can grant credits manually for testing.

The app accepts uploaded video files. YouTube/TikTok URL downloading is not implemented; do not bypass platform restrictions. Use videos you own or have permission to process.

The highlight generator asks the AI to select timestamp ranges from the transcript, then uses FFmpeg to create a compilation. Results depend on transcript quality and are not guaranteed to be a perfect edit.

## 1. Requirements
- Python 3.10+
- FFmpeg installed and available on PATH
- OpenAI API key

## 2. Setup
```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Edit `backend/.env`:
```env
OPENAI_API_KEY=your_api_key
SECRET_KEY=replace_with_a_long_random_secret
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-this-admin-password
```

Start:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Open `http://localhost:8000`.

For phone testing on the same Wi-Fi, open `http://YOUR_COMPUTER_LAN_IP:8000`.

## 3. Admin
On first startup, an admin account is created from `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
Sign in using those credentials, then open Admin from the menu. Change the default password before exposing the app publicly.

## 4. API costs and privacy
- AI transcription and recap use your OpenAI API account and may incur charges.
- Uploaded files and generated files are stored in `backend/data/` on your server.
- This MVP does not yet include automatic retention cleanup, cloud object storage, payment processing, or a production job queue.
- Add HTTPS, rate limits, backups, malware scanning, monitoring, and privacy/retention terms before public launch.

## 5. Production hardening still needed
- Payment gateway integration (requires your merchant account and keys; until connected, Admin must activate Premium manually)
- Email verification and password reset
- Cloud storage and background task queue
- Per-user upload quotas and abuse protection
- Automatic cleanup and audit logging
- Stronger admin role management and deployment security review

## Free and Premium rules
- Free accounts can start up to 3 video jobs per Myanmar calendar day (UTC+06:30).
- Premium accounts are not subject to the daily 3-job limit. Fair-use limits and AI cost controls should be added before public launch.
- The admin can enable/disable Premium for a user from the Admin Dashboard API (`POST /api/admin/premium`).
- Automatic paid upgrades are not enabled until a payment provider and merchant credentials are configured.


## Premium subscription price
- Premium monthly subscription price selected by the site owner: **20,000 MMK / month**.
- This is a displayed business price only. Automatic payment collection and subscription renewal are not implemented until a payment provider/merchant account is connected.
- Until then, the admin manually activates Premium after independently verifying payment.
