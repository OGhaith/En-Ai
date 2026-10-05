import { useEffect } from 'react';
import { useAgent, useSessionContext } from '@livekit/components-react';
import { toastAlert } from '@/components/livekit/alert-toast';

export function useAgentErrors() {
  const agent = useAgent();
  const { isConnected, end } = useSessionContext();
  useEffect(() => {
    if (isConnected && agent.state === 'failed') {
      toastAlert({
        title: 'Could not load the voice agent',
        description: (
          <p>{agent.failureReasons.join(' ') || 'Check the voice services and try again.'}</p>
        ),
      });
      void end();
    }
  }, [agent, isConnected, end]);
}
