# Mini App — DISABLED

The Mini App mock was removed because it had:
- Hardcoded fake balance (1475₽)
- No real API integration
- No initData HMAC validation (security risk)

To re-enable, implement:
1. Real API endpoints for balance/booking data
2. Telegram initData HMAC validation
3. Proper authentication flow
