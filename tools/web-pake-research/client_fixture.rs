// SPDX-License-Identifier: GPL-3.0-or-later
//! Synthetic client/setup fixture. Never linked into the verifier target.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::{rand_core::{Rng, UnwrapErr}, rngs::SysRng},
    ClientLogin, ClientLoginFinishParameters, ClientRegistration, ClientRegistrationFinishParameters,
    CredentialResponse, Identifiers, ServerRegistration, ServerSetup,
};
use profile::Suite;
use std::{io::{self, Read, Write}, process::ExitCode};
use zeroize::{Zeroize, Zeroizing};

fn run() -> Option<()> {
    let (mut input, mut output) = (io::stdin().lock(), io::stdout().lock());
    let mut start = [0; 97];
    input.read_exact(&mut start).ok()?;
    let size = match start[0] {
        0 | 1 => 24,
        2 => 37,
        3 => 1024,
        _ => return None,
    };
    let ids = Identifiers { client: Some(&start[33..65]), server: Some(&start[65..]) };
    let mut rng = UnwrapErr(SysRng);
    let mut password = Zeroizing::new([0; 1024]);
    if start[0] >= 2 {
        // Public test vectors only: the separate browser harness can derive
        // the same synthetic bytes locally without sending any password.
        for (i, byte) in password[..size].iter_mut().enumerate() {
            *byte = b'A' + (i % 26) as u8;
        }
    } else {
        rng.fill_bytes(&mut password[..size]);
    }
    let setup = ServerSetup::<Suite>::new(&mut rng);
    let request = ClientRegistration::<Suite>::start(&mut rng, &password[..size]).ok()?;
    let response = ServerRegistration::<Suite>::start(&setup, request.message, &start[1..33]).ok()?;
    let mut upload = request.state.finish(
        &mut rng,
        &password[..size],
        response.message,
        ClientRegistrationFinishParameters::new(ids, None),
    ).ok()?;
    upload.export_key.zeroize();
    let record = ServerRegistration::<Suite>::finish(upload.message);
    password[0] ^= u8::from(start[0] == 1); // Select a wrong synthetic password for login.
    let login = ClientLogin::<Suite>::start(&mut rng, &password[..size]).ok()?;
    let setup_wire = Zeroizing::new(setup.serialize());
    let record_wire = Zeroizing::new(record.serialize());
    output.write_all(setup_wire.as_slice()).ok()?;
    output.write_all(record_wire.as_slice()).ok()?;
    output.write_all(login.message.serialize().as_slice()).ok()?;
    output.flush().ok()?;
    drop((setup, record, setup_wire, record_wire));

    let mut finish = [0; 416];
    // The test driver may close the pipe when KE2 was refused.
    if let Err(error) = input.read_exact(&mut finish) {
        return (error.kind() == io::ErrorKind::UnexpectedEof).then_some(());
    }
    if input.read(&mut [0]).ok()? != 0 {
        return None;
    }
    let ids = Identifiers { client: Some(&finish[..32]), server: Some(&finish[32..64]) };
    let ke2 = CredentialResponse::deserialize(&finish[96..]).ok()?;
    let result = login.state.finish(
        &mut rng,
        &password[..size],
        ke2,
        ClientLoginFinishParameters::new(Some(&finish[64..96]), ids, None),
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
