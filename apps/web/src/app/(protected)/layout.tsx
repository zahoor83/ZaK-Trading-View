import { redirect } from "next/navigation";

import { SessionProvider } from "@/components/session-context";
import { getAuthenticatedUser } from "@/lib/server/session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export default async function ProtectedLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const user = await getAuthenticatedUser();

  if (!user) {
    redirect("/login");
  }

  return (
    <SessionProvider user={user}>
      {children}
    </SessionProvider>
  );
}