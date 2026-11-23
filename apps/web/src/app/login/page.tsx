export const dynamic = "force-dynamic";

export default function LoginPage() {
  return (
    <main className="login-page">
      <section className="login-panel">
        <div className="eyebrow">ZAK TRADING VIEW</div>
        <h1>Sign in</h1>
        <p className="welcome-description">
          Authenticate securely to access your market intelligence workspace.
        </p>
        <form action="/api/auth/oidc/login" method="post">
          <button className="login-submit" type="submit">
            Continue with secure sign-in
          </button>
        </form>
      </section>
    </main>
  );
}