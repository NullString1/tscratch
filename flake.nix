{
  description = "Dynamic Tiled Compound Scratchpads for Pyprland / Hyprland";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    systems.url = "github:nix-systems/default";
  };

  outputs =
    {
      self,
      nixpkgs,
      systems,
      ...
    }:
    let
      eachSystem = nixpkgs.lib.genAttrs (import systems);
    in
    {
      packages = eachSystem (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ self.overlays.default ];
          };
        in
        {
          default = pkgs.tscratch;
          tscratch = pkgs.tscratch;
          pyprland = pkgs.pyprland;
          pyprland-tscratch = pkgs.pyprland-tscratch;
        }
      );

      overlays.default = final: prev: {
        tscratch = final.callPackage ./package.nix { };

        # Overrides pyprland to include tscratch in its Python environment
        pyprland = prev.pyprland.overridePythonAttrs (old: {
          propagatedBuildInputs = (old.propagatedBuildInputs or [ ]) ++ [ final.tscratch ];
        });

        # Also available explicitly as pyprland-tscratch
        pyprland-tscratch = final.pyprland;
      };

      nixosModules.default =
        { ... }:
        {
          nixpkgs.overlays = [ self.overlays.default ];
        };

      homeModules.default =
        { ... }:
        {
          nixpkgs.overlays = [ self.overlays.default ];
        };

      homeManagerModules.default = self.homeModules.default;
    };
}
