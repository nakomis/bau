import type { ReactNode } from 'react';
import { safeHref } from '../lib/format';

/** An external link, or plain text when the URL is not https. */
export default function ExtLink({
  href,
  className,
  children,
}: {
  href: string | undefined;
  className?: string;
  children: ReactNode;
}) {
  const safe = safeHref(href);
  if (!safe) return <span className={className}>{children}</span>;
  return (
    <a className={className} href={safe} rel="noopener noreferrer">
      {children}
    </a>
  );
}
