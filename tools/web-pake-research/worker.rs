// SPDX-License-Identifier: GPL-3.0-or-later
//! Disabled research worker for `oc_box.web_pake_worker`, one exchange per process.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::{rand_core::UnwrapErr, rngs::SysRng},
    CredentialFinalization, CredentialRequest, Identifiers, ServerLogin, ServerLoginParameters,
    ServerRegistration, ServerSetup,
};
use profile::Suite;
use std::{
    io::{self, Read, Write},
    process::ExitCode,
};
use zeroize::{Zeroize, Zeroizing};

const START: &[u8; 6] = b"OCOP\x01\x01";
const FINISH: &[u8; 6] = b"OCOP\x01\x02";
const SETUP: usize = 128;
const RECORD: usize = 192;
const BINDING: usize = 32;
const KE1: usize = 96;
const KE2: usize = 320;
const KE3: usize = 64;

fn read_field<const N: usize>(input: &mut impl Read) -> io::Result<[u8; N]> {
    let mut field = [0; N];
    input.read_exact(&mut field)?;
    Ok(field)
}

fn read_byte(input: &mut impl Read) -> io::Result<Option<u8>> {
    let mut field = [0];
    loop {
        match input.read(&mut field) {
            Ok(0) => return Ok(None),
            Ok(_) => return Ok(Some(field[0])),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

fn reply(
    output: &mut impl Write,
    operation: &[u8; 6],
    ok: bool,
    payload: &[u8],
) -> io::Result<()> {
    output.write_all(operation)?;
    output.write_all(&[u8::from(!ok)])?;
    output.write_all(payload)?;
    output.flush()
}

fn exchange(input: &mut impl Read, output: &mut impl Write) -> io::Result<()> {
    if read_field(input)? != *START {
        return Err(io::ErrorKind::InvalidData.into());
    }

    let mut setup = Zeroizing::new([0; SETUP]);
    input.read_exact(setup.as_mut())?;
    let [present] = read_field(input)?;
    let mut record = Zeroizing::new([0; RECORD]);
    input.read_exact(record.as_mut())?;
    let credential: [u8; BINDING] = read_field(input)?;
    let client: [u8; BINDING] = read_field(input)?;
    let verifier: [u8; BINDING] = read_field(input)?;
    let context: [u8; BINDING] = read_field(input)?;
    let request: [u8; KE1] = read_field(input)?;
    let params = || ServerLoginParameters {
        context: Some(&context),
        identifiers: Identifiers {
            client: Some(&client),
            server: Some(&verifier),
        },
    };

    let result = (|| {
        let setup = ServerSetup::<Suite>::deserialize(setup.as_ref()).ok()?;
        let record = match present {
            0 if record.iter().all(|&byte| byte == 0) => None,
            1 => Some(ServerRegistration::<Suite>::deserialize(record.as_ref()).ok()?),
            _ => return None,
        };
        let request = CredentialRequest::deserialize(&request).ok()?;
        ServerLogin::start(&mut UnwrapErr(SysRng), &setup, record, request, &credential, params())
            .ok()
    })();
    drop((setup, record));

    let Some(result) = result else {
        return reply(output, START, false, &[]);
    };
    let ke2 = result.message.serialize();
    if ke2.len() != KE2 {
        return Err(io::ErrorKind::InvalidData.into());
    }
    reply(output, START, true, ke2.as_slice())?;

    // EOF without a FINISH is a cancellation. A partial, wrong or overlong
    // frame cannot produce an ACK. The parent closes stdin after sending KE3.
    let Some(first) = read_byte(input)? else {
        return Ok(());
    };
    let mut operation = [first; 6];
    input.read_exact(&mut operation[1..])?;
    if operation != *FINISH {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut proof = Zeroizing::new([0; KE3]);
    input.read_exact(proof.as_mut())?;
    if read_byte(input)?.is_some() {
        return Err(io::ErrorKind::InvalidData.into());
    }

    let verified = CredentialFinalization::deserialize(proof.as_ref())
        .ok()
        .and_then(|message| result.state.finish(message, params()).ok())
        .is_some_and(|mut result| {
            result.session_key.zeroize();
            true
        });
    // A fake record may generate an ordinary KE2, but never a success ACK.
    reply(output, FINISH, present == 1 && verified, &[])
}

fn main() -> ExitCode {
    // No diagnostics containing frame data or implementation errors, including
    // on an entropy failure (the pinned API uses an infallible RNG wrapper).
    std::panic::set_hook(Box::new(|_| {}));
    if std::env::args_os().nth(1).is_some() {
        return ExitCode::FAILURE;
    }
    match exchange(&mut io::stdin().lock(), &mut io::stdout().lock()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::FAILURE,
    }
}
