const jwt = require('jsonwebtoken');

/** Portal `_token` cookie checks, kept free of heavy imports so request middleware can use them */
const PORTAL_COOKIE = '_token';
const getAudience = () => process.env.PORTAL_JWT_AUDIENCE || 'akashx';

/** Email from a valid portal `_token` cookie, or null */
const getPortalEmail = (req) => {
  const token = req.cookies?.[PORTAL_COOKIE];
  if (!token || !process.env.PORTAL_JWT_SECRET) {
    return null;
  }
  try {
    const payload = jwt.verify(token, process.env.PORTAL_JWT_SECRET, {
      algorithms: ['HS256'],
      audience: getAudience(),
    });
    return typeof payload.email === 'string' ? payload.email.trim().toLowerCase() : null;
  } catch {
    return null;
  }
};

/**
 * True when a portal user's LibreChat session no longer matches the portal: they logged
 * out of the portal (no valid `_token`) or logged in there as someone else.
 */
const isPortalSessionStale = (req, user) =>
  !!process.env.PORTAL_JWT_SECRET &&
  user?.provider === 'portal' &&
  getPortalEmail(req) !== user.email;

module.exports = { PORTAL_COOKIE, getAudience, isPortalSessionStale };
