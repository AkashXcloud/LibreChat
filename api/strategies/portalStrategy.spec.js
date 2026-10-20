const jwt = require('jsonwebtoken');

const mockPortalLogin = jest.fn((accessToken, refreshToken, idToken, profile, cb) =>
  cb(null, { email: profile.email }),
);
const mockSocialLogin = jest.fn(() => mockPortalLogin);
jest.mock('./socialLogin', () => mockSocialLogin);

const SECRET = 'test-portal-secret';
process.env.PORTAL_JWT_SECRET = SECRET;
const portalStrategy = require('./portalStrategy');
/** Captured at load time: jest `clearMocks` resets call history before each test */
const [provider, getProfileDetails] = mockSocialLogin.mock.calls[0];

const now = () => Math.floor(Date.now() / 1000);
/** Same claims and algorithm the portal uses for the `_token` cookie */
const portalToken = (overrides = {}, secret = SECRET) =>
  jwt.sign(
    {
      email: 'jane@akashx.ai',
      firstName: 'Jane',
      lastName: 'Doe',
      aud: 'akashx',
      iat: now(),
      exp: now() + 3600,
      ...overrides,
    },
    secret,
  );

function authenticate(cookies) {
  const strategy = portalStrategy();
  return new Promise((resolve) => {
    strategy.success = (user) => resolve({ user });
    strategy.fail = (info) => resolve({ failed: true, info });
    strategy.error = (error) => resolve({ error });
    strategy.authenticate({ cookies, headers: {} }, {});
  });
}

describe('portalStrategy', () => {
  it('logs in through socialLogin with the profile from a valid _token cookie', async () => {
    const result = await authenticate({ _token: portalToken() });

    expect(result.user).toEqual({ email: 'jane@akashx.ai' });
    expect(provider).toBe('portal');
    const profile = { email: 'jane@akashx.ai', firstName: 'Jane', lastName: 'Doe' };
    expect(getProfileDetails({ profile })).toEqual({
      email: 'jane@akashx.ai',
      username: 'Jane',
      name: 'Jane Doe',
      emailVerified: true,
    });
  });

  it.each([
    ['no cookie', {}],
    ['wrong secret', { _token: portalToken({}, 'other-secret') }],
    ['wrong audience', { _token: portalToken({ aud: 'jupyter_hub' }) }],
    ['expired token', { _token: portalToken({ iat: now() - 7200, exp: now() - 3600 }) }],
    ['missing email', { _token: portalToken({ email: undefined }) }],
  ])('rejects %s', async (_name, cookies) => {
    const result = await authenticate(cookies);

    expect(result.failed).toBe(true);
    expect(mockPortalLogin).not.toHaveBeenCalled();
  });
});
