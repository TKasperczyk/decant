{
  description = "decant - Selective offline compaction for Claude Code sessions";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    devenv.url = "github:cachix/devenv/v1.6.1";
  };

  outputs = inputs@{ self, nixpkgs, flake-parts, devenv, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [
        devenv.flakeModule
      ];

      systems = nixpkgs.lib.systems.flakeExposed;

      perSystem = { config, self', inputs', pkgs, system, ... }:
        let
          python = pkgs.python312;
        in
        {
          # `nix run` — runs the decant CLI
          packages.default = python.pkgs.buildPythonApplication {
            pname = "decant";
            version = "0.1.0";
            pyproject = true;

            src = ./.;

            build-system = [
              python.pkgs.hatchling
            ];

            dependencies = [
              python.pkgs.anthropic
            ];

            meta = with pkgs.lib; {
              description = "Selective offline compaction for Claude Code sessions";
              homepage = "https://github.com/TKasperczyk/decant";
              license = licenses.mit;
              mainProgram = "decant";
            };
          };

          # `nix develop` — dev shell with uv, matching quantm conventions
          devenv.shells.default = {
            devenv.root = toString ./.;

            languages.python = {
              enable = true;
              package = python;
              uv = {
                enable = true;
                sync.enable = false;
              };
            };

            scripts = {
              uv_sync.exec = ''uv sync --all-extras --prerelease=allow --dev'';
            };

            packages = with pkgs; [
              ruff
            ];

            enterShell = ''
              echo "🗜  decant dev environment"
              echo "Python: $(python --version)"
              echo "uv:     $(uv --version)"
              echo ""

              if [ ! -d ".devenv/state/venv" ]; then
                echo "📦 Creating venv..."
                uv venv .devenv/state/venv
              fi
              source .devenv/state/venv/bin/activate

              if [ -f ".env" ]; then
                set -a; source .env; set +a
              fi

              export PYTHONPATH="$PWD/src:$PYTHONPATH"

              echo "Run: uv_sync  — install/refresh dependencies"
              echo "Run: decant --help"
            '';
          };
        };
    };
}
