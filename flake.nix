{
  description = "Moodle Session Extractor & Grader";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      browsers = pkgs.playwright-driver.browsers.override {
        withFirefox = false;
        withWebkit = false;
      };
      
      pythonEnv = pkgs.python3.withPackages (ps: with ps; [
        playwright
        requests
      ]);
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ pythonEnv ];
        env = {
          PLAYWRIGHT_BROWSERS_PATH = "${browsers}";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        };
      };
    };
}
