{
  lib,
  stdenvNoCC,
  python3,
}:

stdenvNoCC.mkDerivation {
  pname = "tscratch";
  version = "0.1.0";

  src = ./.;

  dontBuild = true;

  installPhase = ''
    runHook preInstall

    # Site-packages for python importlib resolution
    mkdir -p $out/${python3.sitePackages}
    cp tscratch.py $out/${python3.sitePackages}/tscratch.py

    # Standard shared plugin directory for Pyprland
    mkdir -p $out/share/pyprland/plugins
    cp tscratch.py $out/share/pyprland/plugins/tscratch.py

    runHook postInstall
  '';

  meta = with lib; {
    description = "Dynamic Tiled Compound Scratchpads plugin for Pyprland";
    homepage = "https://github.com/nullstring1/tscratch";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}
