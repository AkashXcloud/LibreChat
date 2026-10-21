const jwt = require('jsonwebtoken');
const { logger, runAsSystem } = require('@librechat/data-schemas');
const { getPortalEmail } = require('~/strategies/portalStrategy');
const { getUserById } = require('~/models');

/**
 * Runs before session refresh. A portal user's LibreChat session stays valid only while
 * the portal `_token` cookie belongs to the same user: logging out of the portal, or
 * logging in there as someone else, ends it. The client then returns to /login, which
 * sends the browser through /oauth/portal again.
 */
const checkPortalSession = async (req, res, next) => {
  const refreshToken = req.cookies?.refreshToken;
  if (!process.env.PORTAL_JWT_SECRET || !refreshToken) {
    return next();
  }

  let user;
  try {
    const { id } = jwt.verify(refreshToken, process.env.JWT_REFRESH_SECRET);
    user = await runAsSystem(() => getUserById(id, 'provider email'));
  } catch {
    /** Invalid refresh tokens are rejected by the refresh controller as usual */
    return next();
  }

  if (user?.provider !== 'portal' || getPortalEmail(req) === user.email) {
    return next();
  }

  logger.info(`[checkPortalSession] Portal session ended or changed user: userId=${user._id}`);
  res.clearCookie('refreshToken');
  res.clearCookie('token_provider');
  return res.status(401).send('Portal session ended');
};

module.exports = checkPortalSession;
