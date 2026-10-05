import { MicrophoneIcon, SpinnerIcon } from '@phosphor-icons/react/dist/ssr';
import { Button } from '@/components/livekit/button';

interface WelcomeViewProps {
  startButtonText: string;
  onStartCall: () => void;
  connecting: boolean;
  error: string;
}

export const WelcomeView = ({
  startButtonText,
  onStartCall,
  connecting,
  error,
  ref,
}: React.ComponentProps<'div'> & WelcomeViewProps) => {
  return (
    <div ref={ref}>
      <section className="bg-background flex flex-col items-center justify-center gap-4 text-center">
        <div className="bg-primary/10 text-primary flex h-14 w-14 items-center justify-center rounded-full">
          <MicrophoneIcon weight="fill" size={26} />
        </div>

        <p className="text-foreground max-w-prose leading-6 font-medium">
          Start a voice call. Speak naturally, and the agent replies when you pause.
        </p>

        <Button
          variant="primary"
          size="lg"
          onClick={onStartCall}
          disabled={connecting}
          className="mt-1 w-48 font-medium"
        >
          {connecting ? (
            <span className="flex items-center justify-center gap-2">
              <SpinnerIcon className="animate-spin" weight="bold" /> Connecting…
            </span>
          ) : (
            startButtonText
          )}
        </Button>
        {error && (
          <p role="alert" className="text-destructive max-w-md text-sm">
            {error}
          </p>
        )}
      </section>

      <div className="fixed bottom-5 left-0 flex w-full items-center justify-center">
        <p className="text-muted-foreground max-w-prose pt-1 text-xs leading-5 font-normal text-pretty md:text-sm">
          Your microphone stays on during the call. You can interrupt the reply at any time.
        </p>
      </div>
    </div>
  );
};
