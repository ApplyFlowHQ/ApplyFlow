import React, { useState } from 'react';
import ReactDOM from 'react-dom/client';
import { registerSW } from 'virtual:pwa-register';
import App from './App';
import './index.css';

const Root: React.FC = () => {
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [updateFn, setUpdateFn] = useState<(() => void) | null>(null);

  React.useEffect(() => {
    const updateSW = registerSW({
      onNeedRefresh() {
        setNeedsRefresh(true);
        setUpdateFn(() => () => updateSW(true));
      },
      onOfflineReady() {
        // Static app shell is cached; API calls remain online-only.
      },
    });
  }, []);

  return <App pwaNeedsRefresh={needsRefresh} onUpdatePwa={() => updateFn?.()} />;
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
