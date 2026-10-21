const jwt = require('jsonwebtoken');

jest.mock('@librechat/data-schemas', () => ({
  logger: { info: jest.fn() },
  runAsSystem: (fn) => fn(),
}));
jest.mock('~/models', () => ({ getUserById: jest.fn() }));
jest.mock('~/strategies/socialLogin', () => jest.fn(() => jest.fn()));

const { getUserById } = require('~/models');
const checkPortalSession = require('./checkPortalSession');

const PORTAL_SECRET = 'portal-secret';
const REFRESH_SECRET = 'refresh-secret';
const refreshCookie = (id = 'user-1') => jwt.sign({ id }, REFRESH_SECRET);
const portalCookie = (email) => jwt.sign({ email, aud: 'akashx' }, PORTAL_SECRET);

function run(cookies) {
  const res = {
    clearCookie: jest.fn(),
    status: jest.fn().mockReturnThis(),
    send: jest.fn().mockReturnThis(),
  };
  const next = jest.fn();
  return checkPortalSession({ cookies }, res, next).then(() => ({ res, next }));
}

describe('checkPortalSession', () => {
  beforeEach(() => {
    process.env.PORTAL_JWT_SECRET = PORTAL_SECRET;
    process.env.JWT_REFRESH_SECRET = REFRESH_SECRET;
    getUserById.mockResolvedValue({ _id: 'user-1', provider: 'portal', email: 'user1@akashx.ai' });
  });

  it('keeps the session when the portal cookie is for the same user', async () => {
    const { next, res } = await run({
      refreshToken: refreshCookie(),
      _token: portalCookie('User1@akashx.ai'),
    });
    expect(next).toHaveBeenCalled();
    expect(res.status).not.toHaveBeenCalled();
  });

  it.each([
    ['logged out of the portal (no _token)', {}],
    ['logged in to the portal as another user', { _token: portalCookie('user2@akashx.ai') }],
    ['invalid portal cookie', { _token: 'not-a-token' }],
  ])('ends the session when %s', async (_name, portalCookies) => {
    const { next, res } = await run({ refreshToken: refreshCookie(), ...portalCookies });
    expect(next).not.toHaveBeenCalled();
    expect(res.clearCookie).toHaveBeenCalledWith('refreshToken');
    expect(res.status).toHaveBeenCalledWith(401);
  });

  it('does not affect users from other providers', async () => {
    getUserById.mockResolvedValue({ _id: 'user-1', provider: 'local', email: 'a@b.c' });
    const { next } = await run({ refreshToken: refreshCookie() });
    expect(next).toHaveBeenCalled();
  });

  it('leaves invalid refresh tokens to the refresh controller', async () => {
    const { next } = await run({ refreshToken: 'garbage' });
    expect(next).toHaveBeenCalled();
    expect(getUserById).not.toHaveBeenCalled();
  });

  it('does nothing when portal login is not configured', async () => {
    delete process.env.PORTAL_JWT_SECRET;
    const { next } = await run({ refreshToken: refreshCookie() });
    expect(next).toHaveBeenCalled();
  });
});
