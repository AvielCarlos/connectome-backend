# Feedback Screenshot Storage

Global feedback accepts `screenshot_data_url` from the app, but screenshots are stored outside Postgres when possible.

## Development

Default config stores decoded screenshots under `storage/feedback-screenshots/...` and records:

- `app_feedback.screenshot_key`
- `app_feedback.screenshot_url` (`file://...` unless `FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL` is set)
- `contributions.attachment_urls` when a public URL exists

## Production / R2 / S3

Set these environment variables:

```env
FEEDBACK_SCREENSHOT_STORAGE_BACKEND=s3
FEEDBACK_SCREENSHOT_S3_BUCKET=your-bucket
FEEDBACK_SCREENSHOT_S3_REGION=auto
FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID=...
FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY=...
FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL=https://your-public-domain.example.com
```

AWS S3 can omit `FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL` and use a normal region like `us-west-2`.

## Failure behaviour

Screenshot storage is intentionally non-fatal. If decoding/upload fails, the feedback text is still inserted into `app_feedback`, and the error is captured in `metadata.screenshot_storage_error`.
