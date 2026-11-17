# Ashigaru 3.x as RPMs: `ashigaru` is the CLI and the Python package, for seat
# images, bootc images and plain hosts; `ashigaru-desk` adds the service and its
# web stack. Built by .github/workflows/rpm.yml through packaging/build.sh, which
# passes the version from VERSION; dependencies come from pyproject.toml.

Name:           ashigaru
Version:        %{?ashigaru_version}%{!?ashigaru_version:0}
Release:        1%{?dist}
Summary:        Command-line client for the Ashigaru Desk
License:        AGPL-3.0-or-later
URL:            https://github.com/crunchtools/ashigaru
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros

%global _description %{expand:
Ashigaru keeps one queue for every source of change, with one owner per item.
The Desk is the service that holds it; the ashigaru command and the Claude Code
hooks that call it are its clients over the Desk's REST API.}

%description %_description

%package desk
Summary:        The Ashigaru Desk service
Requires:       %{name} = %{version}-%{release}
Requires:       %{name}+desk = %{version}-%{release}

%description desk %_description

This package adds the service, ashigaru-desk, and its web stack.

%pyproject_extras_subpkg -n %{name} desk

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -x desk

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files ashigaru
# Every console script but the service's belongs to the CLI package, so a new
# entry point in pyproject.toml ships without an edit here.
for bin in %{buildroot}%{_bindir}/*; do
    [ "${bin##*/}" = ashigaru-desk ] || echo "%{_bindir}/${bin##*/}" >> %{pyproject_files}
done

%check
%pyproject_check_import

%files -f %{pyproject_files}
%license LICENSE
%doc README.md

%files desk
%{_bindir}/ashigaru-desk

%changelog
* Tue Nov 17 2026 Ashigaru <noreply@crunchtools.com> - 0-1
- Built from the release tag; see CHANGELOG.md.
