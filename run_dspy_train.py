#!/usr/bin/env python3
"""
DSPy training wrapper: reads DEEPSEEK_API_KEY from ~/.hermes/.env,
sets OPENAI_API_KEY + OPENAI_BASE_URL, runs dspy_optimizer.py --train.
"""
import os
import subprocess
import sys


def main():
    # Read DeepSeek key from Hermes .env
    env_path = os.path.expanduser('~/.hermes/.env')
    api_key = None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    if key.strip() == 'DEEPSEEK_API_KEY':
                        api_key = val.strip().strip('"').strip("'")
                        break
    except FileNotFoundError:
        print('⚠️ DSPy: ~/.hermes/.env not found', flush=True)
        sys.exit(1)

    if not api_key or api_key == '***':
        print('⚠️ DSPy: DEEPSEEK_API_KEY not set in ~/.hermes/.env — обучение пропущено', flush=True)
        sys.exit(0)

    os.environ['OPENAI_API_KEY'] = api_key
    os.environ['OPENAI_BASE_URL'] = 'https://api.deepseek.com/v1'

    print(f'[DSPy] Starting training with DeepSeek (deepseek-chat)...', flush=True)
    result = subprocess.run(
        [sys.executable, 'dspy_optimizer.py', '--train',
         '--model', 'openai/deepseek-chat'],
        cwd=os.path.dirname(os.path.abspath(__file__)) or '.',
        capture_output=True, text=True, timeout=600,
    )
    print(result.stdout, flush=True)
    if result.stderr:
        print(result.stderr, flush=True)
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
