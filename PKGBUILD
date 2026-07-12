# Maintainer: Ray0rf1re
pkgname=facegate
pkgver=0.2.0
pkgrel=1
pkgdesc="Face unlock (RGB+IR) for Logitech webcams via PAM, Howdy-style"
arch=('any')
url="https://github.com/minerofthesoal/facegate"
license=('MIT')
depends=('python' 'python-numpy' 'opencv' 'python-pam' 'v4l-utils')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools' 'git')
options=('!strip' '!debug')
source=("git+https://github.com/minerofthesoal/facegate.git#tag=v$pkgver")
sha256sums=('SKIP')

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation --outdir "$srcdir/dist"
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" "$srcdir"/dist/*.whl
}
