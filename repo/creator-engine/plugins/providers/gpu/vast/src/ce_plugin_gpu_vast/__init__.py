"""Vast.ai GPU provider (§25): `vast` rents marketplace instances (on-demand, or interruptible when
enabled). Paid: it refuses to provision unless the operator enabled paid provisioning (`allow_paid`)
after the owner approved the spend (§41), and a price ceiling (`max_price_per_hour_usd`) is set."""
