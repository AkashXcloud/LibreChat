# AkashX portal login: deployment checklist

This applies to the AkashX fork, branch `feature/portal-token-login`.

LibreChat signs users in through the AkashX client portal (Superset). The portal sets a `_token` JWT cookie (HS256, `aud=akashx`) on the shared parent domain. LibreChat checks it, then finds or creates the user by email. Airflow uses the same contract.

## Requirements
- LibreChat must be served on a host under the portal's cookie domain (`WORKSPACE_DOMAIN_NAME`), e.g. `librechat-<tenant>.sbnew.cloud.akashx.ai`. Otherwise the browser never sends `_token`.
- The image must be built from this branch. The official LibreChat image does not contain portal login.

## 1. Secret (not in Helm values)
Add `PORTAL_JWT_SECRET` to the Secret named in `global.librechat.existingSecretName` (default `librechat-credentials-env`).
- **Value:** the portal's `AUTH_TOKEN_SECRET`. This is the same value Airflow uses as `AIRFLOW__AKASHX_CLIENT_PORTAL__JWT_SECRET`.
- Copy it without printing it:
```bash
B64=$(kubectl get secret airflow-env-secrets -n <ns> -o jsonpath='{.data.AIRFLOW__AKASHX_CLIENT_PORTAL__JWT_SECRET}')
kubectl patch secret librechat-credentials-env -n <ns> --type merge -p "{\"data\":{\"PORTAL_JWT_SECRET\":\"$B64\"}}"
```
- If the portal secret is ever rotated, update it here too, along with Airflow's.

## 2. Helm values
```yaml
image:
  registry: 091020070662.dkr.ecr.us-east-1.amazonaws.com
  repository: akashx-librechat
  tag: <build of this branch>

librechat:
  configEnv:
    ALLOW_EMAIL_LOGIN: "false"
    ALLOW_REGISTRATION: "false"
    ALLOW_SOCIAL_LOGIN: "true"          # required: login providers load only when this is on
    ALLOW_SOCIAL_REGISTRATION: "true"   # first portal visit creates the LibreChat user
    PORTAL_LOGIN_URL: "https://client-portal-<tenant>.sbnew.cloud.akashx.ai/login"
    PORTAL_LOGOUT_URL: "https://client-portal-<tenant>.sbnew.cloud.akashx.ai/logout"
    REFRESH_TOKEN_EXPIRY: "86400000"    # 24 h, same as the portal cookie

updateStrategy:
  type: Recreate   # the images volume is ReadWriteOnce; RollingUpdate can hang on Multi-Attach
```
- Optional: `PORTAL_JWT_AUDIENCE` (default `akashx`).
- Keep these lines in the **single** values file used for every `helm upgrade` of this release. A deploy from a values file without them turns portal login off.

## 3. Verify after deploy
```bash
H=https://librechat-<tenant>.sbnew.cloud.akashx.ai
curl -s $H/api/config | grep -o '"portalLoginEnabled":[a-z]*'            # "portalLoginEnabled":true
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' $H/oauth/portal   # 302 to the portal /login
```
Then in a browser:
1. Log into the portal and open LibreChat. You should land in LibreChat already logged in.
2. Log out of the portal. Your next action in LibreChat should go to the portal login.
3. Log into the portal as another user. LibreChat should switch to that user.

## Notes
- **New users** get the `USER` role. Promote admins once in MongoDB:
  `db.users.updateOne({email: "<email>"}, {$set: {role: "ADMIN"}})`
- **An existing email/password account** with the same email as a portal user blocks that user's portal login (LibreChat's account-takeover protection). Remove the old account.
