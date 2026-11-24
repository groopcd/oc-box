// SPDX-License-Identifier: GPL-3.0-or-later
//! Standalone synthetic client. Enrollment password bytes never leave this process.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::{rand_core::{Rng, UnwrapErr}, rngs::SysRng},
    ClientLogin, ClientLoginFinishParameters, ClientRegistration, ClientRegistrationFinishParameters,
    CredentialResponse, Identifiers, RegistrationResponse,
};
use profile::Suite;
use std::{
    io::{self, Read, Write},
    process::ExitCode,
};
use zeroize::{Zeroize, Zeroizing};

fn run() -> Option<()> {
    let (mut input, mut output) = (io::stdin().lock(), io::stdout().lock());
    let mut ids = [0; 64];
    input.read_exact(&mut ids).ok()?;
    let identifiers = Identifiers {
        client: Some(&ids[..32]),
        server: Some(&ids[32..]),
    };
    let mut rng = UnwrapErr(SysRng);
    let mut password = Zeroizing::new([0; 24]);
    rng.fill_bytes(password.as_mut());
    let registration = ClientRegistration::<Suite>::start(&mut rng, password.as_ref()).ok()?;
    output.write_all(registration.message.serialize().as_slice()).ok()?;
    output.flush().ok()?;

    let mut response = [0; 64];
    input.read_exact(&mut response).ok()?;
    let response = RegistrationResponse::deserialize(&response).ok()?;
    let mut upload = registration.state.finish(
        &mut rng,
        password.as_ref(),
        response,
        ClientRegistrationFinishParameters::new(identifiers, None),
    ).ok()?;
    upload.export_key.zeroize();
    output.write_all(Zeroizing::new(upload.message.serialize()).as_slice()).ok()?;
    drop(upload);
    let login = ClientLogin::<Suite>::start(&mut rng, password.as_ref()).ok()?;
    output.write_all(login.message.serialize().as_slice()).ok()?;
    output.flush().ok()?;

    let mut frame = [0; 32 + 320];
    input.read_exact(&mut frame).ok()?;
    if input.read(&mut [0]).ok()? != 0 {
        return None;
    }
    let response = CredentialResponse::deserialize(&frame[32..]).ok()?;
    let result = login.state.finish(
        &mut rng,
        password.as_ref(),
        response,
        ClientLoginFinishParameters::new(Some(&frame[..32]), identifiers, None),
    );
    if let Ok(mut result) = result {
        result.session_key.zeroize();
        result.export_key.zeroize();
        output.write_all(&[0]).ok()?;
        output.write_all(result.message.serialize().as_slice()).ok()?;
    } else {
        output.write_all(&[1]).ok()?;
    }
    output.flush().ok()
}

fn main() -> ExitCode {
    std::panic::set_hook(Box::new(|_| {}));
    if std::env::args_os().nth(1).is_none() && run().is_some() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
