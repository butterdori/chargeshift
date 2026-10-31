pkgname=chargeshift
pkgver=0.2.0
pkgrel=2
pkgdesc='Charge-limit scheduling for Framework laptops'
arch=('any')
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'libadwaita>=1.4' 'hicolor-icon-theme' 'polkit' 'systemd' 'tzdata' 'framework-system')
install=chargeshift.install
backup=('etc/chargeshift/hardware.json')
source=()
sha256sums=()

check() {
  cd "$startdir"
  python -m unittest discover -s tests -v
}

package() {
  cd "$startdir"
  install -d "$pkgdir/usr/lib/chargeshift/chargeshift"
  install -m644 chargeshift/*.py "$pkgdir/usr/lib/chargeshift/chargeshift/"
  install -Dm755 bin/chargeshift "$pkgdir/usr/bin/chargeshift"
  install -Dm755 bin/chargeshiftctl "$pkgdir/usr/bin/chargeshiftctl"
  install -Dm755 bin/chargeshiftd "$pkgdir/usr/lib/chargeshift/chargeshiftd"
  install -Dm644 data/chargeshift.service "$pkgdir/usr/lib/systemd/system/chargeshift.service"
  install -Dm644 data/chargeshift-event.service "$pkgdir/usr/lib/systemd/system/chargeshift-event.service"
  install -Dm644 data/chargeshift-event.timer "$pkgdir/usr/lib/systemd/system/chargeshift-event.timer"
  install -Dm644 data/chargeshift.conf "$pkgdir/usr/lib/tmpfiles.d/chargeshift.conf"
  install -Dm644 data/hardware.json "$pkgdir/etc/chargeshift/hardware.json"
  install -Dm644 data/io.github.chargeshift.ChargeShift.conf "$pkgdir/usr/share/dbus-1/system.d/io.github.chargeshift.ChargeShift.conf"
  install -Dm644 data/io.github.chargeshift.policy "$pkgdir/usr/share/polkit-1/actions/io.github.chargeshift.policy"
  install -Dm644 data/io.github.chargeshift.ChargeShift.desktop "$pkgdir/usr/share/applications/io.github.chargeshift.ChargeShift.desktop"
  install -Dm644 data/icons/hicolor/scalable/apps/io.github.chargeshift.ChargeShift.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.chargeshift.ChargeShift.svg"
  install -Dm644 data/icons/hicolor/symbolic/apps/io.github.chargeshift.ChargeShift-symbolic.svg "$pkgdir/usr/share/icons/hicolor/symbolic/apps/io.github.chargeshift.ChargeShift-symbolic.svg"
  install -Dm644 CHANGELOG.md "$pkgdir/usr/share/doc/chargeshift/CHANGELOG.md"
  install -Dm644 README.md "$pkgdir/usr/share/doc/chargeshift/README.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/chargeshift/LICENSE"
}
