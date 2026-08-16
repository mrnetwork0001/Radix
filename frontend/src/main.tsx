import React, { Suspense, lazy, useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';

import './index.css';
import { isAppPath, navigate } from './lib/router';

// Two-route split without a router dependency: the landing must stay
// featherweight, so the dashboard (force-graph and all) only loads when the
// path says so.
const App = lazy(() => import('./App'));
const Landing = lazy(() => import('./Landing'));

function Root() {
  const [inApp, setInApp] = useState(() => isAppPath(window.location.pathname));

  useEffect(() => {
    const onPopState = () => setInApp(isAppPath(window.location.pathname));
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    document.title = inApp ? 'Radix Console' : 'Radix — Supply Chain Sentinel';
  }, [inApp]);

  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center">
          <span className="label-micro animate-breathe text-cyan">loading radix…</span>
        </div>
      }
    >
      {inApp ? <App /> : <Landing onLaunch={() => navigate('/app')} />}
    </Suspense>
  );
}

const container = document.getElementById('root');
if (!container) throw new Error('#root is missing from index.html');

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
