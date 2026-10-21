import type { BuildVersion } from '../types';

export default function Footer({ version }: { version?: BuildVersion }) {
  return (
    <footer className="mt-12 border-t border-line bg-bar px-3 py-2 text-center text-xs text-white/40">
      {version ? `Nakomis BAU ${version.version} (${version.sha})` : 'Nakomis BAU'}
    </footer>
  );
}
