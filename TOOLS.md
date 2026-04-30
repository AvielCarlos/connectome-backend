# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

---

### atdao.org Email (Microsoft 365 via GoDaddy)
- **avi@atdao.org** — Avi's email | pw: $3r54MtNY!98&kq
- **nea@atdao.org** — Nea's email | pw: n-?U7_A7at5Z:yk
- SMTP: smtp.office365.com:587 (STARTTLS)
- IMAP: outlook.office365.com:993
- Webmail: https://outlook.office.com

---

### Stripe
- Old exposed live secret key removed. Stripe deactivated the key ending `b7OPly`; generate/rotate a new live key in Stripe Dashboard before production payment work.
