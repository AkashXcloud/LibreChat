const jwt = require('jsonwebtoken');
const { Strategy: JwtStrategy } = require('passport-jwt');
const socialLogin = require('./socialLogin');

/**
 * AkashX client portal login: the portal sets a `_token` JWT cookie on the shared
 * parent domain (same contract Airflow uses). Users are matched by email.
 */
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

const getProfileDetails = ({ profile }) => ({
  email: profile.email,
  username: profile.firstName,
  name: [profile.firstName, profile.lastName].filter(Boolean).join(' '),
  emailVerified: true,
});

const portalLogin = socialLogin('portal', getProfileDetails);

const portalStrategy = () =>
  new JwtStrategy(
    {
      jwtFromRequest: (req) => req.cookies?.[PORTAL_COOKIE] ?? null,
      secretOrKey: process.env.PORTAL_JWT_SECRET,
      algorithms: ['HS256'],
      audience: getAudience(),
    },
    (payload, done) => {
      if (!payload?.email) {
        return done(null, false);
      }
      return portalLogin(null, null, null, payload, done);
    },
  );

module.exports = portalStrategy;
module.exports.getPortalEmail = getPortalEmail;
