const { Strategy: JwtStrategy } = require('passport-jwt');
const { PORTAL_COOKIE, getAudience } = require('./portalSession');
const socialLogin = require('./socialLogin');

/**
 * AkashX client portal login: the portal sets a `_token` JWT cookie on the shared
 * parent domain (same contract Airflow uses). Users are matched by email.
 */
const getProfileDetails = ({ profile }) => ({
  email: profile.email.trim().toLowerCase(),
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
